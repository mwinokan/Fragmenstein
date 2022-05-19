from typing import (Union, Dict, List)

from rdkit import Chem
from rdkit.Chem import AllChem

from ..monster import Monster
from ..branding import divergent_colors

# courtesy of .legacy
from functools import singledispatchmethod

class WaltonBase:
    color_scales = divergent_colors

    def __init__(self,
                 mols: List[Chem.Mol],
                 aligned: bool = False):
        """
        To initialised from SMILES use the classmethod ``.from_smiles``.
        These are assumed to have a conformer.
        The mols will be assigned a property ``_color`` based on
        the class attribute color_scales. By default it uses ``fragmenstein.branding.divergent_colors``

        :param mols: list of mols
        :param aligned: are they aligned? sets the namesake argument that does nothing ATM
        """
        # ## Mol
        self.mols: List[Chem.Mol] = mols
        # assign index (just in case):
        for idx, mol in enumerate(self.mols):
            mol.SetIntProp('_mol_index', idx)
        # assign color:
        self.color_in()
        # ## Aligned
        self.aligned: bool = aligned
        # ## Computed
        self.merged: Union[None, Chem.Mol] = None

    def color_in(self):
        """
        assigns a _color property to a mol based on color_scales of correct length

        Gets called by ``__init__`` and ``duplicate``.
        """
        color_scale = self.color_scales[len(self.mols)]
        for mol, color in zip(self.mols, color_scale):
            mol.SetProp('_color', color)

    @classmethod
    def from_smiles(cls,
                    aligned: bool = False,
                    add_Hs: bool = False,
                    **name2smiles: Dict[str, str]):
        """
        Load from SMILES.
        provided as named arguments: ``from_smiles(bezene='c1ccccc1',..)``
        """
        mols: List[Chem.Mol] = []
        for name, smiles in name2smiles.items():
            mol = Chem.MolFromSmiles(smiles)
            if add_Hs:
                mol = AllChem.AddHs(mol)
            AllChem.EmbedMolecule(mol)
            mol.SetProp('_Name', name)
            mols.append(mol)
        return cls(mols=mols, aligned=aligned)

    def __call__(self, color='#a9a9a9', minimize: bool = False, **combine_kwargs) -> Chem.Mol:  # darkgrey
        """
        Calls Monster to do the merger.
        Filling the attribute ``merged`` w/ a Chem.Mol.
        Also returns it.
        """
        # neogreen '#39ff14'
        # joining_cutoff= 5
        monster = Monster(list(map(AllChem.RemoveHs, self.mols))).combine(**combine_kwargs)
        if minimize:
            monster.mmff_minimize()
        self.merged = monster.positioned_mol
        self.merged.SetProp('_color', color)
        return self.merged

    def duplicate(self, mol_idx: int):
        """
        Duplicate the molecule at a given index.
        And fix colours.
        """
        self.mols.append(Chem.Mol(self.get_mol(mol_idx)))
        self.color_in()

    @singledispatchmethod
    def get_mol(self, mol_idx: int) -> Chem.Mol:
        """
        Type dispatched method:

        * Gets the molecule in ``.mols`` with index ``mol_idx``
        * returns the molecule provided as ``mol``

        The latter route is not used within the module
        but does mean one could pass a mol instead of a mol_idx...
        """
        assert isinstance(mol_idx, int)
        assert len(self.mols) > mol_idx, f'The instance of Walton has only {len(self.mols)}, so cannot get {mol_idx}.'
        return self.mols[mol_idx]

    @get_mol.register
    def _(self, mol: Chem.Mol) -> Chem.Mol:
        return mol
