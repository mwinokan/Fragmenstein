from ._utility import _MonsterUtil
from rdkit import Chem
from rdkit.Chem import AllChem, rdqueries, rdMolAlign, rdMolTransforms
from typing import Optional, List, Union, Tuple, Dict
from warnings import warn
from dataclasses import dataclass
import numpy as np
import numpy.typing as npt
from ..error import FragmensteinError

@dataclass
class MinizationOutcome:
    success: bool
    mol: Chem.Mol
    ideal: Chem.Mol
    U_pre: float = float('nan')
    U_post: float = float('nan')
    delta: float = float('nan')


class _MonsterFF(_MonsterUtil):

    def mmff_minimize(self,
                      mol: Optional[Chem.Mol] = None,
                      neighborhood: Union[Chem.Mol, None] = None,
                      ff_max_displacement: float = 0.,
                      ff_constraint: int = 10,
                      ff_max_iterations: int=200,
                      ff_cutoff: float = 100.,
                      allow_lax: bool = True,
                      prevent_cis: bool = True,
                      ) -> MinizationOutcome:
        """
        Minimises a mol, or self.positioned_mol if not provided, with MMFF constrained to ff_max_displacement Å.
        Gets called by Victor if the flag .monster_mmff_minimisation is true during PDB template construction.

        :param mol: Molecule to minimise. If None, self.positioned_mol is used.
        :param neighborhood: Protein neighboorhood (ignored if None)
        :param ff_max_displacement: Distance threshold (Å) for atomic positions mapped to hits for  MMFF constrains.
                            if NaN then fixed point constraints (no movement) are used.
                            This is passed as maxDispl to MMFFAddPositionConstraint.
        :param ff_constraint: Force constant for MMFF constraints.
        :param ff_cutoff: kcal/mol diff value to consider a failed minimisation.
        :param allow_lax: If True and the minimisation fails, the constraints are halved and the minimisation is rerun.
        :return: None

        Note that most methods calling this via Victor
        now use its ``.settings['ff_max_displacement']`` and ``.settings['ff_constraint']``
        and do not use the defaults.
        """
        # ## input fixes
        if mol is None and self.positioned_mol is None:
            raise ValueError('No valid molecule')
        elif mol is None:
            mol = self.positioned_mol
        else:
            pass  # mol is fine
        # ## prep
        success: bool
        fixed_mode = str(ff_max_displacement).lower() == 'nan'
        mol = AllChem.AddHs(mol, addCoords=True)
        # protect
        for atom in mol.GetAtomsMatchingQuery(Chem.rdqueries.HasPropQueryAtom('_IsDummy')):
            atom.SetAtomicNum(8)
        combo, fixed_idxs = self._prep_combined(mol, neighborhood)
        ideal: Chem.Mol = self.make_ideal_mol(mol, ff_minimise=True)
        ideal_E: float = ideal.GetDoubleProp('Energy')
        # ## Start FF
        p: AllChem.MMFFMolProperties = AllChem.MMFFGetMoleculeProperties(combo, 'MMFF94')
        if p is None:
            self.journal.error(f'MMFF cannot work on a molecule that has errors!')
            return MinizationOutcome(success=False, mol=mol, ideal=ideal)
        ff: AllChem.ForceField = AllChem.MMFFGetMoleculeForceField(combo, p, ignoreInterfragInteractions=False)
        if ff is None:
            return MinizationOutcome(success=False, mol=mol, ideal=ideal,
                                     U_post=float('nan'), U_pre=float('nan'), delta=0)
        # restrain
        # mol not combo here:
        conserved: List[Chem.Atom] = self._get_conserved_for_ff_step(mol)
        # weird corner case
        if len(conserved) == mol.GetNumAtoms() and fixed_mode:
            ff.Initialize()
            dU: float = ff.CalcEnergy()  # noqa internal energy
            self.journal.info('No novel atoms found in fixed_mode (ff_max_displacement == NaN), ' + \
                                 'this is probably a mistake')
            # nothing to do...
            return MinizationOutcome(success=True, mol=mol, ideal=ideal, U_post=dU, U_pre=dU, delta=0)
        # constrain or freeze
        restrained = self._add_constraint_to_ff_step(mol=mol,
                                                     ff=ff,
                                                     conserved=conserved,
                                                     fixed_mode=fixed_mode,
                                                     fixed_idxs=fixed_idxs,
                                                     ff_constraint=ff_constraint,
                                                     ff_max_displacement=ff_max_displacement,
                                                     prevent_cis=prevent_cis)
        # ## Minimize
        try:
            success, dG_pre, dG_post = self._classified_ff_minimize_step(mol=mol,
                                                                         ff=ff,
                                                                         ff_max_iterations=ff_max_iterations)
        except RuntimeError as error:
            self.journal.info(f'MMFF minimisation failed {error.__class__.__name__}: {error}')
            return MinizationOutcome(success=False, mol=mol, ideal=ideal)
        # extract
        new_mol = self.extract_from_neighborhood(combo)
        ligand_E: float = self.MMFF_score(new_mol, delta=False)
        new_mol.SetDoubleProp('Energy', ligand_E)
        # check
        if ligand_E - ideal_E > abs(ff_cutoff):
            success = False  # damn
        if not success and allow_lax:
            self.journal.debug(f'MMFF minimisation failed, trying again with lax constraint {ff_constraint // 5}')
            return self.mmff_minimize(mol,
                                      neighborhood=neighborhood,
                                      ff_max_displacement=ff_max_displacement,
                                      ff_constraint=ff_constraint // 5,
                                      allow_lax=False)
        # deprotect
        for atom in new_mol.GetAtomsMatchingQuery(Chem.rdqueries.HasPropQueryAtom('_IsDummy')):
            atom.SetAtomicNum(0)
        # prevent drift:
        #rdMolAlign.AlignMol(new_mol, mol, atomMap=list(zip(restrained, restrained)))
        self.journal.info(f'MMFF minimisation: {dG_pre:.2f} -> {dG_post:.2f} kcal/mol ' +
                           f'w/ {rdMolAlign.CalcRMS(new_mol, mol)}Å RMSD at ' +
                           f'max displacement={ff_max_displacement} & constraint={ff_constraint}'
                           )
        return MinizationOutcome(success=success,
                                 mol=new_mol,
                                 ideal=ideal,
                                 U_post=dG_post,
                                 U_pre=dG_pre,
                                 delta=dG_post - dG_pre)

    def _get_conserved_for_ff_step(self, mol) -> List[Chem.Atom]:
        # list(mol.GetAtomsMatchingQuery(rdqueries.HasPropQueryAtom('_Novel', negate=True)))
        conserved: List[Chem.Atom] = []
        atom: Chem.Atom
        for atom in mol.GetAtoms():
            if atom.GetAtomicNum() == 1:  # hydrogen
                pass
            elif atom.HasProp('_Novel') and atom.GetBoolProp('_Novel'):
                pass
            elif atom.HasProp('_x') or \
                    (atom.HasProp('_Origin') and atom.GetProp('_Origin') != 'none'):
                conserved.append(atom)
            else:
                pass
        return conserved



    def _add_constraint_to_ff_step(self, mol: Chem.Mol,
                                   ff: AllChem.ForceField, conserved: List[Chem.Atom],
                                   fixed_mode: bool, fixed_idxs: List[int],
                             ff_constraint, ff_max_displacement, prevent_cis) -> List[int]:
        """
        See ``mmff_minimize`` for details.
        """
        restrained = []
        atom: Chem.Atom
        self._add_ff_amide_correction(mol, ff, ff_constraint, prevent_cis)
        for atom in conserved:
            i = atom.GetIdx()
            if atom.GetAtomicNum() == 1:
                # let hydrogens move
                continue
            elif atom.HasProp('_IsAmide') and atom.GetBoolProp('_IsAmide'):
                # amide bonds have their own constraints
                continue
            elif fixed_mode:
                atom.SetProp('_MMFF', 'fixed')
                ff.AddFixedPoint(i)
            elif (atom.GetProp('_isRing') if atom.HasProp('_isRing') else False):
                # be 2-fold more lax with rings
                atom.SetProp('_MMFF', 'ring')
                ff.MMFFAddPositionConstraint(i, maxDispl=ff_max_displacement, forceConstant=ff_constraint/2)
            else:
                # https://github.com/rdkit/rdkit/blob/115317f43e3bdfd73673ca0e4c6b4035aa26a034/Code/ForceField/UFF/PositionConstraint.cpp#L35
                atom.SetProp('_MMFF', 'atom')
                ff.MMFFAddPositionConstraint(i, maxDispl=ff_max_displacement, forceConstant=ff_constraint)
            restrained.append(i)
        # constrain dummy atoms
        atom: Chem.Atom
        for atom in mol.GetAtomsMatchingQuery(rdqueries.HasPropQueryAtom('_IsDummy')):
            i = atom.GetIdx()
            atom.SetProp('_MMFF', 'dummy')
            ff.MMFFAddPositionConstraint(i, maxDispl=0, forceConstant=ff_constraint * 5)
            restrained.append(i)
        for i in fixed_idxs:  # neighborhood is frozen
            ff.AddFixedPoint(i)
        self.post_ff_addition_step(mol, ff)
        return restrained

    def post_ff_addition_step(self, mol: Chem.Mol, ff: AllChem.ForceField):
        """
        THis is an empty method for user created subclasses to add their own constraints to the MMFF minimisation.
        """
        pass

    @staticmethod
    def inspect_amide_torsions(mol):
        """
        The most noticeable torsions are the amide ones.
        This is to describe what is happening.
        """
        amidelike = Chem.MolFromSmarts('*C(=[O,S,N])[N,O]*')
        conf = mol.GetConformer()
        idx2symbol = lambda idx: f'[{mol.GetAtomWithIdx(idx).GetSymbol()}:{idx}]'
        for cprime, calpha, pendant, hetero, descendant in mol.GetSubstructMatches(amidelike):
            print(f'Torsion {idx2symbol(cprime)}-{idx2symbol(calpha)}(=-{idx2symbol(pendant)})-{idx2symbol(hetero)} = ',
                  round(rdMolTransforms.GetDihedralDeg(conf, cprime, calpha, pendant, hetero), 1) )
            print(f'Torsion {idx2symbol(cprime)}-{idx2symbol(calpha)}-{idx2symbol(hetero)}-{idx2symbol(descendant)} = ',
                  round(rdMolTransforms.GetDihedralDeg(conf, cprime, calpha, hetero, descendant), 1) )
            print(f'angles {idx2symbol(cprime)}-{idx2symbol(calpha)}(=-{idx2symbol(pendant)}) = ',
                  round(rdMolTransforms.GetAngleDeg(conf, cprime, calpha, pendant), 1) )
            print(f'angles {idx2symbol(cprime)}-{idx2symbol(calpha)}{idx2symbol(descendant)} = ',
                  round(rdMolTransforms.GetAngleDeg(conf, calpha, hetero, descendant), 1) )

    def _add_torsion_constraint(self, conf, ff, idxs, forceConstant, enforce_180=False, enforce_0=False):
        """
        Formerly cis/trans E/Z were through around, including for hydrogens...
        """
        omega = rdMolTransforms.GetDihedralDeg(conf, *idxs)
        if enforce_0 or (abs(omega) < 90 and not enforce_180): # is_cis
            ff.MMFFAddTorsionConstraint(*idxs, relative=False, minDihedralDeg=-5,
                                        maxDihedralDeg=+5, forceConstant=forceConstant)
        elif omega <= -90:  # trans and negative angle
            # this is silly to split, but the periodicity -179º = 181º is handled weirdly...
            ff.MMFFAddTorsionConstraint(*idxs, relative=False, minDihedralDeg=-185,
                                        maxDihedralDeg=-175, forceConstant=forceConstant)
        else:  # trans and positive angle
            ff.MMFFAddTorsionConstraint(*idxs, relative=False, minDihedralDeg=175,
                                        maxDihedralDeg=185, forceConstant=forceConstant)

    def _add_ff_amide_correction(self, mol: Chem.Mol, ff: AllChem.ForceField, forceConstant: float, prevent_cis: bool):
        """
        The amides are normally fixed by the normalisation... but not always,
        especially when the constraints are forcing mad things.
        This really drives it home.
        """

        conf = mol.GetConformer()
        # ## mark atoms
        amidelike = Chem.MolFromSmarts('*[C!R](=[O,S,N])[N,O]*')
        for idxs in mol.GetSubstructMatches(amidelike):
            for idx in idxs:
                mol.GetAtomWithIdx(idx).SetBoolProp('_IsAmide', True)
        # ## cprime, calpha, pendant, hetero
        # no descendants first
        # add a contraint so cprime, calpha, pendant, hetero are planar.
        # this can only have a dihedral of 180°
        amidelike_no_desc = Chem.MolFromSmarts('*[C!R](=[O,S,N])[N,O]')
        for cprime, calpha, pendant, hetero in mol.GetSubstructMatches(amidelike_no_desc):
            self._add_torsion_constraint(conf, ff, idxs=(cprime, calpha, pendant, hetero),
                                         forceConstant=forceConstant,
                                         enforce_180=True)
            ff.MMFFAddAngleConstraint(cprime, calpha, pendant, relative=False, minAngleDeg=110, maxAngleDeg=130,
                                      forceConstant=forceConstant)
            ff.MMFFAddAngleConstraint(hetero, calpha, pendant, relative=False, minAngleDeg=110, maxAngleDeg=130,
                                      forceConstant=forceConstant)
        # ## calpha, pendant, hetero, descendant
        # descendant = substituent on backbone hetero
        # descendants: this adds the trans / cis problem
        amide_no_prime = Chem.MolFromSmarts('[C!R](=[O,S,N])N*')
        # this time, hetero can only be N (amide, no ester) as there the substituent on an ester is free to rotate
        for calpha, pendant, hetero, descendant in mol.GetSubstructMatches(amide_no_prime):
            n_hydrogens_hetero = sum([neigh.GetAtomicNum() == 1 for neigh in mol.GetAtomWithIdx(hetero).GetNeighbors()])
            if n_hydrogens_hetero == 2:
                # primary amide
                pass # do nothing
            elif n_hydrogens_hetero == 0:
                # there is no E/Z... but there is planarity!
                self._add_torsion_constraint(conf, ff, idxs=(pendant, calpha, hetero, descendant),
                                             forceConstant=forceConstant)
            elif not prevent_cis:
                self._add_torsion_constraint(conf, ff, idxs=(pendant, calpha, hetero, descendant),
                                             forceConstant=forceConstant)
            if mol.GetAtomWithIdx(descendant).GetAtomicNum() == 1: # descendant is hydro
                # secondary amide but we have hydrogen here
                # the hydrogen is the opposite side to the pendant
                self._add_torsion_constraint(conf, ff, idxs=(pendant, calpha, hetero, descendant),
                                             forceConstant=forceConstant,
                                             enforce_180=True)
            else:
                self._add_torsion_constraint(conf, ff, idxs=(pendant, calpha, hetero, descendant),
                                             forceConstant=forceConstant,
                                             enforce_0=True)
            ff.MMFFAddAngleConstraint(calpha, hetero, descendant, relative=False, minAngleDeg=110, maxAngleDeg=130,
                                      forceConstant=forceConstant)
            ff.MMFFAddAngleConstraint(pendant, hetero, descendant, relative=False, minAngleDeg=110, maxAngleDeg=130,
                                      forceConstant=forceConstant)

    def _classified_ff_minimize_step(self, mol: Chem.Mol, ff: AllChem.ForceField, ff_max_iterations: int) -> Tuple[bool, float, float]:
        """
        See ``mmff_minimize`` for details.
        """
        dG_pre = ff.CalcEnergy()  # noqa although yes Gibbs is uppercase, but this is actually U, internal energy
        dG_post = dG_pre  # noqa
        previous_dG = 0.  # noqa
        m = -1
        # this is a bit of a hack, but it works to make sure it's not a flipped plateau-like local minima
        while previous_dG == 0. or previous_dG - dG_post > 0.5:
            previous_dG = dG_post
            m = ff.Minimize(maxIts=ff_max_iterations)
            dG_post = ff.CalcEnergy()
            if m == -1:
                break
        if m == -1:
            self.journal.error('MMFF Minisation could not be started')
            success = False
        elif m == 0:
            self.journal.info('MMFF Minisation was successful')
            success = True
        elif m == 1:
            self.journal.info('MMFF Minisation was run, but the minimisation was not unsuccessful')
            success = False
        else:
            self.journal.critical("Iä! Iä! Cthulhu fhtagn! Ph'nglui mglw'nafh Cthulhu R'lyeh wgah'nagl fhtagn")
            success = False
        return success, dG_pre, dG_post

    def _prep_combined(self, mol, neighborhood) -> Tuple[Chem.Mol, List[int]]:
        # ## protect (DummyMasker could be used here)
        for atom in mol.GetAtomsMatchingQuery(Chem.rdqueries.AtomNumEqualsQueryAtom(0)):
            atom.SetBoolProp('_IsDummy', True)
            atom.SetAtomicNum(16)
        Chem.SanitizeMol(mol)
        # ## Combine with neighborhood
        if neighborhood is not None:
            Chem.SanitizeMol(neighborhood)
            hydroneighborhood = AllChem.AddHs(neighborhood, addCoords=True)
            combo: Chem.Mol = Chem.CombineMols(mol, hydroneighborhood)
            Chem.SanitizeMol(combo)
            fixed_idxs: List[int] = list(range(mol.GetNumAtoms(), combo.GetNumAtoms()))
        else:
            combo = Chem.Mol(mol)
            fixed_idxs: List[int] = []
        self.journal.debug(f'Combined molecule (ligand+neighbourhood) has {combo.GetNumAtoms()} atoms, {fixed_idxs} fixed')
        return combo, fixed_idxs

    def MMFF_score(self, mol: Optional[Chem.Mol] = None, delta: bool = False, mode: str = 'MMFF') -> float:
        """
        Merck force field. Chosen over Universal for no reason at all.

        :param mol: ligand
        :type mol: ``Chem.Mol`` optional. If absent extracts from pose.
        :param delta: report difference from unbound (minimized)
        :type delta: bool
        :param mode: 'MMFF' or 'UFF'
        :type mode: str
        :return: kcal/mol
        :rtype: float

        :warning: This was moved out of Igor. Victor has the method for calling it with igor.mol_from_pose
        """
        if mol is None:
            mol = self.positioned_mol
        try:
            mol = AllChem.AddHs(mol, addCoords=True)  # copy!
            if mode == 'UFF':
                ff = AllChem.UFFGetMoleculeForceField(mol)
            elif mode == 'MMFF':
                p = AllChem.MMFFGetMoleculeProperties(mol, 'MMFF94')
                ff = AllChem.MMFFGetMoleculeForceField(mol, p)
            else:
                raise ValueError(f'Unknown mode: {mode} (choice: MMFF or UFF)')
            ff.Initialize()
            # print(f'MMFF: {ff.CalcEnergy()} kcal/mol')
            if delta:
                pre = ff.CalcEnergy()
                ff.Minimize()
                post = ff.CalcEnergy()
                return pre - post
            else:
                return ff.CalcEnergy()
        except RuntimeError as err:
            self.journal.warning(f'{err.__class__.__name__}: {err} (It is generally due to bad sanitisation)')
            return float('nan')

    @classmethod
    def get_close_indices(cls, query: Chem.Mol, target: Chem.Mol, cutoff: float = 5.) -> List[int]:
        """
        Give an rdkit Chem.Mol ``query`` get the atom idices of ``target`` that are with ``cutoff`` Å.
        """
        combo = Chem.CombineMols(target, query)
        distances: npt.NDArray[np.float64] = AllChem.Get3DDistanceMatrix(combo)
        query2target_dist: npt.NDArray[np.float64] = distances[
            slice(target.GetNumAtoms(), combo.GetNumAtoms(), 1), slice(0, target.GetNumAtoms(), 1)].min(axis=0)
        neighbors: npt.NDArray[np.int64] = np.where(query2target_dist <= cutoff)[0]
        return list(map(int, neighbors))

    @classmethod
    def _get_aromatic_neighbors(cls, atom, accounted):
        neighbor: Chem.Atom
        for neighbor in atom.GetNeighbors():
            neigh_idx: int = neighbor.GetIdx()
            if neighbor.GetIsAromatic() and neigh_idx not in accounted:
                accounted.append(neigh_idx)
                return cls._get_aromatic_neighbors(neighbor, accounted)
        return accounted

    @classmethod
    def extract_atoms(cls, protein: Chem.Mol, keepers: List[int], expand_aromatics: bool = True) -> Chem.Mol:
        """
        Extract the given atom indices (``keepers``) from ``protein``.
        Expanding to full aromatic ring and copying conformers
        """
        pasteboard = Chem.RWMol()
        # ## Expand aromatic rings
        exkeepers = list(keepers)
        if expand_aromatics:
            for idx in keepers:
                atom: Chem.Atom = protein.GetAtomWithIdx(idx)
                if not atom.GetIsAromatic():
                    continue
                exkeepers = cls._get_aromatic_neighbors(atom, exkeepers)
        # ## Add atoms
        prot2paste: Dict[int, int] = {}
        for idx in exkeepers:
            atom: Chem.Atom = protein.GetAtomWithIdx(int(idx))  # no to np.int64
            if not expand_aromatics:
                atom.SetIsAromatic(False)
            prot2paste[idx] = pasteboard.AddAtom(atom)
        # ## Add bonds
        for prot_idx, paste_idx in prot2paste.items():
            atom: Chem.Atom = protein.GetAtomWithIdx(prot_idx)
            for prot_neighbor_idx in [n.GetIdx() for n in atom.GetNeighbors()]:
                if prot_neighbor_idx in prot2paste and prot_neighbor_idx > prot_idx:
                    prot_bond: Chem.Bond = protein.GetBondBetweenAtoms(prot_idx, prot_neighbor_idx)
                    paste_neighneighbor_idx = prot2paste[prot_neighbor_idx]
                    pasteboard.AddBond(paste_idx, paste_neighneighbor_idx,
                                       prot_bond.GetBondType() if expand_aromatics else Chem.BondType.SINGLE)

        pasteboard_conf = Chem.Conformer(len(keepers))
        positions: npt.NDArray = protein.GetConformer().GetPositions()
        for prot_idx, paste_idx in prot2paste.items():
            pasteboard_conf.SetAtomPosition(paste_idx, positions[prot_idx, :])
        pasteboard.AddConformer(pasteboard_conf)
        return pasteboard.GetMol()

    def get_neighborhood(self, apo_block: str, cutoff: float, mol: Optional[Chem.Mol] = None, addHs=True) -> Chem.Mol:
        """
        Get the neighborhood of the protein from the apo_block around the cutoff of the mol.
        Note: The atoms will have a prop ``IsNeighborhood`` which is used after it is combined.
        """
        if mol is None:
            mol = self.positioned_mol
        protein: Chem.Mol = Chem.MolFromPDBBlock(apo_block)
        neighbor_idxs: List[int] = self.get_close_indices(mol, protein, cutoff)
        neighborhood: Chem.Mol = self.extract_atoms(protein, neighbor_idxs)
        AllChem.SanitizeMol(neighborhood, catchErrors=True)
        if addHs:
            neighborhood = AllChem.AddHs(neighborhood, addCoords=True)
        self.journal.debug(f'{cutoff}Å Neighborhood has {neighborhood.GetNumAtoms()} atoms')
        for atom in neighborhood.GetAtoms():
            atom.SetBoolProp('IsNeighborhood', True)
        AllChem.SanitizeMol(neighborhood, catchErrors=True)
        return neighborhood

    def make_ideal_mol(self, mol: Optional[Chem.Mol]=None, ff_minimise: bool=False) -> Chem.Mol:
        if mol is None:
            mol = self.positioned_mol
        ideal = Chem.Mol(mol)
        ideal.SetDoubleProp('Energy', float('nan'))
        AllChem.EmbedMolecule(ideal)
        p: AllChem.MMFFMolProperties = AllChem.MMFFGetMoleculeProperties(ideal, 'MMFF94')
        ff = AllChem.MMFFGetMoleculeForceField(ideal, p)
        if ff is None:
            raise FragmensteinError('Ideal compound failed. Something is wrong with the SMILES')
        ff.Initialize()
        if ff_minimise:
            ff.Minimize()
        energy = ff.CalcEnergy()
        ideal.SetDoubleProp('Energy', energy)
        return ideal

    def extract_from_neighborhood(self, system: Chem.Mol) -> Chem.Mol:
        """
        Given a system of a neighbourhood + ligand extract everything that is not marked ``IsNeighborhood``.
        """
        rwmol = Chem.RWMol(system)
        rwmol.BeginBatchEdit()
        for atom in rwmol.GetAtoms():
            if atom.HasProp('IsNeighborhood'):
                rwmol.RemoveAtom(atom.GetIdx())
        rwmol.CommitBatchEdit()
        # isNeighborhood encompasses Hs... but just in case:
        # this warning happened: 'WARNING: not removing hydrogen atom without neighbors'
        # I have not seen it since, but I got a report of it
        new_mol = sorted(Chem.GetMolFrags(rwmol.GetMol(), asMols=True), key=Chem.Mol.GetNumAtoms, reverse=True)[0]
        return new_mol
