########################################################################################################################

__doc__ = \
    """
This is Fragmenstein proper. and contains the class ``Fragmenstein``.
    """
__author__ = "Matteo Ferla. [Github](https://github.com/matteoferla)"
__email__ = "matteo.ferla@gmail.com"
__date__ = "2019 A.D."
__license__ = "MIT"
__version__ = "0.4"
__citation__ = ""

########################################################################################################################

from typing import Dict, Union, List, Optional, Tuple
from warnings import warn
import json

import numpy as np
from collections import defaultdict

from rdkit import Chem
from rdkit.Chem import AllChem, rdFMCS, rdMolAlign, rdmolops
from rdkit.Geometry.rdGeometry import Point3D

from ._utility_mixin import _FragmensteinUtil


##################################################################

class Fragmenstein(_FragmensteinUtil):
    """
    Given a RDKit molecule and a series of hits it makes a spatially stitched together version of the initial molecule based on the hits.
    The reason is to do place the followup compound to the hits as faithfully as possible regardless of the screaming forcefields.

    * ``.scaffold`` is the combined version of the hits (rdkit.Chem.Mol object).
    * ``.chimera`` is the combined version of the hits, but with differing atoms made to match the followup (rdkit.Chem.Mol object).
    * ``.positioned_mol`` is the desired output (rdkit.Chem.Mol object)

    Note, the hits have to be spatially aligned —i.e. extracted from crystal structures in bond form.

    ``.get_positional_mapping``, which works also as a class method, creates a dictionary of mol_A atom index to mol_B atom index
    based on distance (cutoff 2&Aring;) and not MCS.

    The code works in two broad steps, first a scaffold is made, which is the combination of the hits (by position).
    Then the followup is placed. It is not embedded with constraint embed, which requires the reference molecule to have a valid geometry.
    ``.scaffold`` and ``.chimera`` and ``.positioned_mol`` absolutely do not have this.
    Novel side chains are added by aligning an optimised conformer against the closest 3-4 reference atoms.
    Note that ``.initial_mol`` is not touched. ``.positioned_mol`` may have lost some custom properties, but the atom idices are the same.

    If an atom in a Chem.Mol object is provided via ``attachment`` argument and the molecule contains a dummy atom as
    defined in the ``dummy`` class variable. Namely element R in mol file or * in string is the default.
    """
    dummy_symbol = '*'
    dummy = Chem.MolFromSmiles(dummy_symbol)  #: The virtual atom where the targets attaches
    matching_modes = [dict(atomCompare=rdFMCS.AtomCompare.CompareAny,
                          bondCompare=rdFMCS.BondCompare.CompareAny,
                          ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                          ringMatchesRingOnly=False),
                     dict(atomCompare=rdFMCS.AtomCompare.CompareAny,
                          bondCompare=rdFMCS.BondCompare.CompareOrder,
                          ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                          ringMatchesRingOnly=False),
                     dict(atomCompare=rdFMCS.AtomCompare.CompareElements,
                          bondCompare=rdFMCS.BondCompare.CompareOrder,
                          ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                          ringMatchesRingOnly=False),
                     dict(atomCompare=rdFMCS.AtomCompare.CompareAny,
                          bondCompare=rdFMCS.BondCompare.CompareAny,
                          ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                          ringMatchesRingOnly=True),
                     dict(atomCompare=rdFMCS.AtomCompare.CompareAny,
                          bondCompare=rdFMCS.BondCompare.CompareOrder,
                          ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                          ringMatchesRingOnly=True),
                     dict(atomCompare=rdFMCS.AtomCompare.CompareElements,
                          bondCompare=rdFMCS.BondCompare.CompareOrder,
                          ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                          ringMatchesRingOnly=True)]

    def __init__(self, mol: Chem.Mol, hits: List[Chem.Mol], attachment: Optional[Chem.Mol] = None,
                 debug_draw: bool = False):
        # starting attributes
        self.logbook = {}
        self.initial_mol = mol  # untouched.
        if self.initial_mol.HasSubstructMatch(self.dummy) and attachment:
            self.attachement = attachment
        elif self.initial_mol.HasSubstructMatch(self.dummy):
            warn('No attachment atom provided but dummy atom present --- ignoring.')
            self.attachement = None
        elif attachment:
            warn('Attachment atom provided but dummy atom not present --- ignoring.')
            self.attachement = None
        else:
            self.attachement = None
        # Chem.RemoveHs(self.initial_mol)
        self.hits = hits  # list of hits
        self._debug_draw = debug_draw  # Jupyter notebook only.
        self.unmatched = []
        # derived attributes
        unrefined_scaffold = self.merge_hits()  # merger of hits
        self.scaffold = self.posthoc_refine(unrefined_scaffold)
        self.chimera = self.make_chimera()  # merger of hits but with atoms made to match the to-be-aligned mol
        self.positioned_mol = self.place_followup()  # to-be-aligned is aligned!

    def merge_pair(self, scaffold: Chem.Mol, fragmentanda: Chem.Mol, mapping: Optional=None) -> Chem.Mol:
        """
        To specify attachments use ``.merge``.

        :param scaffold: mol to be added to.
        :param fragmentanda: mol to be fragmented
        :param mapping: see ``get_positional_mapping``. Optional
        :return:
        """
        done_already = []
        # these are hit indices:
        fp = self._fragment_pairs(scaffold, fragmentanda, mapping)
        # confusingly thes fragment pairs
        for anchor_index, attachment_details in fp.items():
            if anchor_index in done_already:
                continue
            # fix rings.
            uniques = {atom.GetIdx() for atom in fragmentanda.GetAtoms() if 'overlapping' not in atom.GetProp('_Category')}
            team = self._recruit_team(fragmentanda, anchor_index, uniques)
            other_attachments = list((team & set(fp.keys())) - {anchor_index})
            other_attachment_details = []
            for other in other_attachments:
                other_attachment_details.append(fp[other])
                done_already.append(other)
            scaffold = self._merge_part(scaffold, fragmentanda,
                                  anchor_index=anchor_index,
                                  attachment_details=attachment_details,
                                  other_attachments = other_attachments,
                                  other_attachment_details=other_attachment_details)
        name_A = scaffold.GetProp('_Name')
        name_B = fragmentanda.GetProp('_Name')
        scaffold.SetProp('_Name', f'{name_A}-{name_B}')
        return scaffold

    def merge_hits(self, hits: Optional[List[Chem.Mol]]=None) -> Chem.Mol:
        """
        Recursively stick the hits together and average the positions.
        :param hits: optionally give a hit list, else uses the attribute ``.hits``.
        :return: the rdkit.Chem.Mol object that will fill ``.scaffold``
        """
        if hits is None:
            hits = sorted(self.hits, key=lambda h: h.GetNumAtoms(), reverse=True)
        for hi, hit in enumerate(hits):
            # fallback naming.
            if not hit.HasProp('_Name') or hit.GetProp('_Name').strip() == '':
                hit.SetProp('_Name', f'hit{hi}')
        scaffold = Chem.Mol(hits[0])
        save_for_later = []
        for fragmentanda in hits[1:]:
            try:
                scaffold = self.merge_pair(scaffold, fragmentanda)
            except ConnectionError:
                save_for_later.append(fragmentanda)
        for fragmentanda in save_for_later:
            try:
                scaffold = self.merge_pair(scaffold, fragmentanda)
            except ConnectionError:
                self.unmatched.append(fragmentanda.GetProp("_Name"))
                warn(f'Hit {fragmentanda.GetProp("_Name")} has no connections! Skipping!')
        return scaffold

    def make_chimera(self) -> Chem.Mol:
        """
        This is to avoid extreme corner corner cases. E.g. here the MCS is ringMatchesRingOnly=True and AtomCompare.CompareAny,
        while for the positioning this is not the case.

        :return:
        """
        # get the matches
        atom_map, mode = self.get_mcs_mapping(self.scaffold, self.initial_mol)
        self.logbook['scaffold-followup'] = {**{k: str(v) for k, v in mode.items()}, 'N_atoms': len(atom_map)}
        if self._debug_draw:
            self.draw_nicely(self.initial_mol, highlightAtoms=atom_map.values())
        ## make the scaffold more like the followup to avoid weird matches.
        chimera = Chem.RWMol(self.scaffold)
        for scaff_ai, follow_ai in atom_map.items():
            if self.scaffold.GetAtomWithIdx(scaff_ai).GetSymbol() != self.initial_mol.GetAtomWithIdx(follow_ai).GetSymbol():
                v = {'F': 1, 'Br': 1, 'Cl': 1, 'H': 1, 'B': 3, 'C': 4, 'N': 3, 'O': 2, 'S': 2, 'Se': 2, 'P': 6}
                wanted = self.initial_mol.GetAtomWithIdx(follow_ai)
                if wanted.GetSymbol() == '*': # all good then!
                    continue
                owned = self.scaffold.GetAtomWithIdx(scaff_ai)
                diff_valance = owned.GetExplicitValence() - v[wanted.GetSymbol()]
                if wanted.GetSymbol() in ('F', 'Br', 'Cl', 'C', 'H') and diff_valance > 0:
                    continue  # cannot change this.
                elif owned.GetExplicitValence() > 4 and wanted.GetSymbol() not in ('P',):
                    continue
                else:
                    newatom = Chem.Atom(wanted)
                    stdev = chimera.GetAtomWithIdx(scaff_ai).GetDoubleProp('_Stdev')
                    newatom.SetDoubleProp('_Stdev', stdev)
                    origin = chimera.GetAtomWithIdx(scaff_ai).GetProp('_Origin')
                    newatom.SetProp('_Origin', origin)
                    chimera.ReplaceAtom(scaff_ai, newatom)
                    if diff_valance > 0:
                        chimera.GetAtomWithIdx(scaff_ai).SetFormalCharge(diff_valance)
        try:
            chimera.UpdatePropertyCache()
        except Chem.AtomValenceException as err:
            warn('Valance issue' + str(err))
        return chimera

    def place_followup(self, mol: Chem.Mol = None) -> Chem.Mol:
        # Note none of this malarkey: AllChem.MMFFOptimizeMolecule(ref)
        # prealignment
        if mol is None:
            mol = self.initial_mol
        sextant = Chem.Mol(mol)
        Chem.SanitizeMol(sextant)
        AllChem.EmbedMolecule(sextant)
        AllChem.MMFFOptimizeMolecule(sextant)
        atom_map, mode = self.get_mcs_mapping(mol, self.chimera)
        self.logbook['followup-chimera'] = {**{k: str(v) for k, v in mode.items()}, 'N_atoms': len(atom_map)}
        rdMolAlign.AlignMol(sextant, self.chimera, atomMap=list(atom_map.items()), maxIters=500)
        if self._debug_draw:
            self.draw_nicely(mol, highlightAtoms=dict(atom_map).keys())
            self.draw_nicely(self.chimera, highlightAtoms=dict(atom_map).values())
        putty = Chem.Mol(sextant)
        pconf = putty.GetConformer()
        chimera_conf = self.chimera.GetConformer()
        uniques = set()  # unique atoms in followup
        for i in range(putty.GetNumAtoms()):
            if i in atom_map:
                ci = atom_map[i]
                stdev = self.chimera.GetAtomWithIdx(ci).GetDoubleProp('_Stdev')
                origin = self.chimera.GetAtomWithIdx(ci).GetProp('_Origin')
                putty.GetAtomWithIdx(i).SetDoubleProp('_Stdev', stdev)
                putty.GetAtomWithIdx(i).SetProp('_Origin', origin)
                pconf.SetAtomPosition(i, chimera_conf.GetAtomPosition(ci))
            else:
                uniques.add(i)
                putty.GetAtomWithIdx(i).SetDoubleProp('_Stdev', 0.)
                putty.GetAtomWithIdx(i).SetProp('_Origin', 'none')
        # we be using a sextant for dead reckoning!
        categories = self._categorise(sextant, uniques)
        if self._debug_draw:
            print('internal', categories['internals'])
        done_already = []
        for unique_idx in categories['pairs']:  # attachment unique indices
            if unique_idx in done_already:
                continue
            sights = set()
            for pd in categories['pairs'][unique_idx]:
                first_sight = pd['idx']
                sights.add((first_sight, first_sight))
                neighs = [i.GetIdx() for i in sextant.GetAtomWithIdx(first_sight).GetNeighbors() if
                          i.GetIdx() not in uniques]
                for n in neighs:
                    sights.add((n, n))
            team = self._recruit_team(mol, unique_idx, categories['uniques'])
            if self.attachement and list(categories['dummies']) and list(categories['dummies'])[0] in team:
                r = list(categories['dummies'])[0]
                pconf.SetAtomPosition(r, self.attachement.GetConformer().GetAtomPosition(0))
                sights.add((r, r))
            rdMolAlign.AlignMol(sextant, putty, atomMap=list(sights), maxIters=500)
            sconf = sextant.GetConformer()
            if self._debug_draw:
                print(f'alignment atoms for {unique_idx} ({team}): {sights}')
                self.draw_nicely(sextant, highlightAtoms=[a for a, b in sights])
            for atom_idx in team:
                pconf.SetAtomPosition(atom_idx, sconf.GetAtomPosition(atom_idx))
            # the ring problem does not apply here but would result in rejiggling atoms.
            other_attachments = (team & set(categories['pairs'].keys())) - {unique_idx}
            for other in other_attachments:
                done_already.append(other)

        AllChem.SanitizeMol(putty)
        return putty #positioned_mol

    def _merge_part(self, scaffold: Chem.Mol, fragmentanda: Chem.Mol, anchor_index: int,
              attachment_details: List[Dict],
              other_attachments: List[int],
              other_attachment_details: List[List[Dict]]) -> Chem.Mol:
        """
        This does the messy work for merge_pair.

        :param scaffold:
        :param fragmentanda:
        :param anchor_index:
        :param attachment_details:
        :param other_attachments:
        :param other_attachment_details:
        :return:
        """
        for detail in attachment_details:
            attachment_index = detail['idx_F']  # fragmentanda attachment_index
            scaffold_attachment_index = detail['idx_S']
            bond_type = detail['type']
            bonds_to_frag = [fragmentanda.GetBondBetweenAtoms(anchor_index, attachment_index).GetIdx()]
            bonds_to_frag += [fragmentanda.GetBondBetweenAtoms(oi, oad[0]['idx_F']).GetIdx() for oi, oad in zip(other_attachments, other_attachment_details)]
            if self._debug_draw:
                print(other_attachments)
                print(other_attachment_details)
            f = Chem.FragmentOnBonds(fragmentanda,
                                     bonds_to_frag,
                                     addDummies=False)
            frag_split = []
            fragmols = Chem.GetMolFrags(f, asMols=True, fragsMolAtomMapping=frag_split, sanitizeFrags=False)
            if self._debug_draw:
                print('Fragment splits')
                print(frag_split)
            # Get the fragment of interest.
            ii = 0
            for mol_N, indices in enumerate(frag_split):
                if anchor_index in indices:
                    break
                ii += len(indices)
            else:
                raise Exception
            frag = fragmols[mol_N]
            frag_anchor_index = indices.index(anchor_index)
            if self._debug_draw:
                print('Fragment to add')
                self.draw_nicely(frag)
            combo = Chem.RWMol(rdmolops.CombineMols(scaffold, frag))
            scaffold_anchor_index = frag_anchor_index + scaffold.GetNumAtoms()
            if self._debug_draw:
                print('Pre-merger')
                print(scaffold_anchor_index, scaffold_attachment_index, anchor_index, scaffold.GetNumAtoms())
                self.draw_nicely(combo)
            combo.AddBond(scaffold_anchor_index, scaffold_attachment_index, bond_type)
        for oi, oad in zip(other_attachments, other_attachment_details):
            bond_type = oad[0]['type']
            scaffold_attachment_index = oad[0]['idx_S']
            scaffold_anchor_index = indices.index(oi) + scaffold.GetNumAtoms()
            combo.AddBond(scaffold_anchor_index, scaffold_attachment_index, bond_type)
            Chem.SanitizeMol(combo,
                             sanitizeOps=Chem.rdmolops.SanitizeFlags.SANITIZE_ADJUSTHS +
                                         Chem.rdmolops.SanitizeFlags.SANITIZE_SETAROMATICITY,
                             catchErrors=True)
            if self._debug_draw:
                print('Merged')
                self.draw_nicely(combo)
            scaffold = combo
        return scaffold

    def _fragment_pairs(self, scaffold: Chem.Mol, fragmentanda: Chem.Mol, A2B_mapping: Optional=None) -> Dict[int, List[Dict]]:
        """
        Returns

            {4: [{'idx': 5,
                   'type': rdkit.Chem.rdchem.BondType.SINGLE,
                   'idx_F': 5,
                   'idx_S': 1}], ...}

        which is slight more than {5: [{'idx': 4, 'type': rdkit.Chem.rdchem.BondType.SINGLE}], ... from categories

        required for self.merge, the key is the index of anchoring atom.

        Calls get_positional_mapping

        :param scaffold: mol to be added to.
        :param fragmentanda: mol to be fragmented
        :param A2B_mapping: see ``get_positional_mapping``
        :return:
        """
        if A2B_mapping is None:
            A2B_mapping = self.get_positional_mapping(scaffold, fragmentanda)
        get_key = lambda d, v: list(d.keys())[list(d.values()).index(v)]
        if len(A2B_mapping) == 0:
            raise ConnectionError
        uniques = set(range(fragmentanda.GetNumAtoms())) - set(A2B_mapping.values())
        categories = self._categorise(fragmentanda, uniques)
        pairs = categories['pairs']
        for p in pairs:  # pairs:Dict[List[Dict]]
            for pp in pairs[p]:
                pp['idx_F'] = pp['idx']  # less ambiguous: fragmentanda index
                pp['idx_S'] = get_key(A2B_mapping, pp['idx'])  # scaffold index
        return pairs

    @classmethod
    def get_positional_mapping(cls, mol_A: Chem.Mol, mol_B: Chem.Mol, cutoff=2) -> Dict[int, int]:
        """
        Returns a map to convert overlapping atom of A onto B
        Cutoff 2 &Aring;.

        :param mol_A: first molecule (Chem.Mol) will form keys
        :param mol_B: second molecule (Chem.Mol) will form values
        :return: dictionary mol A atom idx -> mol B atom idx.
        """
        mols = [mol_A, mol_B]
        confs = [m.GetConformers()[0] for m in mols]
        distance = lambda a, b: ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5
        m = []
        for i in range(mols[0].GetNumAtoms()):
            v = []
            for j in range(mols[1].GetNumAtoms()):
                d = distance(confs[0].GetAtomPosition(i), confs[1].GetAtomPosition(j))
                v.append(d)
            m.append(v)
        dm = np.array(m)
        ## find the closest
        mapping = {}
        while 1 == 1:
            d = np.amin(dm)
            if d > cutoff:
                break
            w = np.where(dm == d)
            f, s = w[0][0], w[1][0]
            mapping[int(f)] = int(s)  # np.int64 --> int
            dm[f, :] = np.ones(dm.shape[1]) * 999
            dm[:, s] = np.ones(dm.shape[0]) * 999
        return mapping

    def _categorise(self, mol: Chem.Mol, uniques: set) -> Dict[str, Union[set, Dict]]:
        """
        What do the novel atoms do in terms of connectivity.
        Complicated dict output (called ``categories`` in the methods). Really ought to be SetProp of the atoms.

        * ``uniques`` are set of atoms to classify on
        * ``internals`` are unique atoms that are connected solely to unique atoms
        * ``attachments`` are non-unique atoms to which a unique atom connects
        * ``pairs`` is a dict of unique atom idx --> dict of ``idx`` --> attachment idx and ``type`` bond type.

        :param mol: molecule to describe
        :param uniques: set of indices that are new to this molecule
        :return:
        """
        #
        pairs = {}
        internals = set()
        attachments = set()
        dummies = set()
        for i in uniques: # novel atoms
            unique_atom = mol.GetAtomWithIdx(i)
            if unique_atom.GetSymbol() == self.dummy_symbol:
                dummies.add(i)
            neighbours = {n.GetIdx() for n in unique_atom.GetNeighbors()}
            if len(neighbours - uniques) == 0: # unlessone of the connections is not unique.
                internals.add(i)
            else:
                i_attached = neighbours - uniques
                attachments |= i_attached
                pairs[i] = [{'idx': j,
                             'type': mol.GetBondBetweenAtoms(i, j).GetBondType()} for j in i_attached]
        anchors = uniques - internals
        # store for safekeeping
        for atom in mol.GetAtoms():
            i = atom.GetIdx()
            if i in internals:  # novel and not connected
                atom.SetProp('_Category', 'internal')
            elif i in attachments:  # not-novel but connected
                atom.SetProp('_Category', 'overlapping-attachment')
            elif i in pairs: # dict not set tho
                atom.SetProp('_Category', 'internal-attachment')
            else:  # overlapping
                atom.SetProp('_Category', 'overlapping')
        if self._debug_draw:
            high = list(internals) + list(attachments) + list(anchors)
            color = {**{i: (0, 0.8, 0) for i in internals},
                     **{i: (0, 0, 0.8) for i in attachments},
                     **{i: (0.8, 0, 0.8) for i in anchors}}
            print('Purple: anchor atoms, Blue: attachments, Green: internals')
            self.draw_nicely(mol, highlightAtoms=high, highlightAtomColors=color)
            print({atom.GetIdx(): atom.GetProp('_Category') for atom in mol.GetAtoms()})
        return dict(uniques=uniques,
                    internals=internals,
                    attachments=attachments,
                    pairs=pairs,
                    dummies=dummies
                    )

    def posthoc_refine(self, scaffold):
        """
        Averages the overlapping atoms.

        :param scaffold:
        :return:
        """
        refined = Chem.RWMol(scaffold)
        refconf = refined.GetConformer()
        positions = defaultdict(list) # coordinates
        equivalence = defaultdict(list) # atom indices of hits.
        for h in self.hits:
            hc = h.GetConformer()
            for k, v in self.get_positional_mapping(scaffold, h).items():
                positions[k].append([hc.GetAtomPosition(v).x, hc.GetAtomPosition(v).y, hc.GetAtomPosition(v).z])
                equivalence[k].append(f'{h.GetProp("_Name")}.{v}')
        for i in range(scaffold.GetNumAtoms()):
            if len(positions[i]) == 0:
                refined.GetAtomWithIdx(i).SetDoubleProp('_Stdev', 0.)
                refined.GetAtomWithIdx(i).SetProp('_Origin', 'none')
                # warn(f'Atom {i}  {scaffold.GetAtomWithIdx(i).GetSymbol}/{refined.GetAtomWithIdx(i).GetSymbol} '+ \
                #     'in scaffold that has no positions.')
            else:
                p = np.mean(np.array(positions[i]), axis=0).astype(float)
                sd = np.mean(np.std(np.array(positions[i]), axis=0)).astype(float) # TODO this seems a bit dodgy.
                refined.GetAtomWithIdx(i).SetProp('_Origin', json.dumps(equivalence[i]))
                refined.GetAtomWithIdx(i).SetDoubleProp('_Stdev', sd)
                refconf.SetAtomPosition(i, Point3D(p[0], p[1], p[2]))
        Chem.SanitizeMol(refined,
                         sanitizeOps=Chem.rdmolops.SanitizeFlags.SANITIZE_ADJUSTHS +
                                     Chem.rdmolops.SanitizeFlags.SANITIZE_SETAROMATICITY,
                         catchErrors=True)
        return refined

    def get_mcs_mapping(self, molA, molB) -> Tuple[Dict[int, int], dict]:
        """
        This is a weird method. It does a strict MCS match.
        And then it uses laxer searches and finds the case where a lax search includes the strict search.

        :param molA: query molecule
        :param molB: target/ref molecule
        :return: mapping and mode
        """
        strict = self._get_atom_maps(molA, molB, atomCompare=rdFMCS.AtomCompare.CompareElements,
                                     bondCompare=rdFMCS.BondCompare.CompareOrder,
                                     ringMatchesRingOnly=True,
                                     ringCompare=rdFMCS.RingCompare.PermissiveRingFusion,
                                     matchChiralTag=True)
        for mode in self.matching_modes:
            lax = self._get_atom_maps(molA, molB, **mode)
            # remove the lax matches that disobey
            neolax = [l for l in lax if any([len(set(s) - set(l)) == 0 for s in strict])]
            if len(neolax) == 0:
                continue
            else:
                return dict(neolax[0]), mode
        else:
            raise ValueError('This is chemically impossible.')

    def _get_atom_maps(self, molA, molB, **mode) -> List[List[Tuple[int, int]]]:
        mcs = rdFMCS.FindMCS([molA, molB], **mode)
        common = Chem.MolFromSmarts(mcs.smartsString)
        matches = []
        # prevent a dummy to match a non-dummy, which can happen when the mode is super lax.
        is_dummy = lambda mol, at: mol.GetAtomWithIdx(at).GetSymbol() == '*'
        all_bar_dummy = lambda Aat, Bat:  (is_dummy(molA, Aat) and is_dummy(molB, Bat)) or not (is_dummy(molA, Aat) or is_dummy(molB, Bat))
        for molA_match in molA.GetSubstructMatches(common):
            for molB_match in molB.GetSubstructMatches(common):
                matches.append([(molA_at, molB_at) for molA_at, molB_at in zip(molA_match, molB_match) if all_bar_dummy(molA_at, molB_at)])
        return matches

    def _get_atom_map(self, molA, molB, **mode) -> List[Tuple[int, int]]:
        return self._get_atom_maps(molA, molB, **mode)[0]

    def _recruit_team(self, mol: Chem.Mol, starting: int, uniques: set, team: Optional[set] = None) -> set:
        if team is None:
            team = set()
        team.add(starting)
        for atom in mol.GetAtomWithIdx(starting).GetNeighbors():
            i = atom.GetIdx()
            if i in uniques and i not in team:
                team = self._recruit_team(mol, i, uniques, team)
        return team

    def pretweak(self) -> None:
        """
        What if the fragments were prealigned slightly? Really bad things.

        :return:
        """
        warn('This method is unreliable. Do not use it')
        ref = self.hits[0]
        for target in self.hits[1:]:
            A2B = list(self.get_positional_mapping(target, ref, 0.5).items())
            if A2B:
                rdMolAlign.AlignMol(target, ref, atomMap=A2B, maxIters=500)
            else:
                warn(f'No overlap? {A2B}')