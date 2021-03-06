from aiida.common import CalcInfo, CodeInfo
from aiida.plugins import DataFactory
from aiida_phonopy.common.raw_parsers import (get_BORN_txt,
                                              get_phonopy_conf_file_txt,
                                              get_poscar_txt)

Dict = DataFactory('dict')
StructureData = DataFactory('structure')
ArrayData = DataFactory('array')


class BasePhonopyCalculation(object):
    """
    A basic plugin for calculating force constants using Phonopy.
    Requirement: the node should be able to import phonopy if NAC is used
    """

    _INPUT_CELL = 'POSCAR'
    _INPUT_FORCE_SETS = 'FORCE_SETS'
    _INPUT_NAC = 'BORN'

    @classmethod
    def _baseclass_use_methods(cls, spec):
        spec.input('settings', valid_type=Dict, required=True,
                   help=('Use a node that specifies the phonopy '
                         'parameters for the namelists'))
        spec.input('structure', valid_type=StructureData, required=True,
                   help=('Use a node for the structure'))
        spec.input('force_sets', valid_type=ArrayData, required=False,
                   help=('Use a node that specifies the force_sets '
                         'array for the namelists'))
        spec.input('nac_params', valid_type=ArrayData, required=False,
                   help=('Use a node for the Non-analitical '
                         'corrections parameters arrays'))
        spec.input('primitive', valid_type=StructureData,
                   required=False, help=('Use a node for the structure'))
        spec.input('metadata.options.withmpi', valid_type=bool, default=False)

    def _create_additional_files(self, folder):
        pass

    def prepare_for_submission(self, folder):
        """Create the input files from the input nodes passed to this instance of the `CalcJob`.

        :param folder: an `aiida.common.folders.Folder` to temporarily write files on disk
        :return: `aiida.common.datastructures.CalcInfo` instance
        """

        self.logger.info("prepare_for_submission")

        self._internal_retrieve_list = []
        self._additional_cmd_params = []
        self._calculation_cmd = []

        settings = self.inputs.settings
        structure = self.inputs.structure
        code = self.inputs.code

        ##############################
        # END OF INITIAL INPUT CHECK #
        ##############################

        # ================= prepare the python input files =================

        self._create_additional_files(folder)

        cell_txt = get_poscar_txt(structure)
        input_txt = get_phonopy_conf_file_txt(settings)

        input_filename = folder.get_abs_path(
            self.inputs.metadata.options.input_filename)
        with open(input_filename, 'w') as infile:
            infile.write(input_txt)

        cell_filename = folder.get_abs_path(self._INPUT_CELL)
        with open(cell_filename, 'w') as infile:
            infile.write(cell_txt)

        if ('nac_params' in self.inputs and
            'primitive' in self.inputs):
            born_txt = get_BORN_txt(
                self.inputs.nac_params,
                self.inputs.primitive,
                settings['symmetry_tolerance'])

            nac_filename = folder.get_abs_path(self._INPUT_NAC)
            with open(nac_filename, 'w') as infile:
                infile.write(born_txt)
            for params in self._additional_cmd_params:
                params.append('--nac')

        # ============================ calcinfo ===============================

        local_copy_list = []
        remote_copy_list = []

        calcinfo = CalcInfo()

        calcinfo.uuid = self.uuid

        # Empty command line by default
        calcinfo.local_copy_list = local_copy_list
        calcinfo.remote_copy_list = remote_copy_list

        # Retrieve files
        calcinfo.retrieve_list = self._internal_retrieve_list

        calcinfo.codes_info = []
        for default_params, additional_params in zip(
                self._calculation_cmd, self._additional_cmd_params):
            codeinfo = CodeInfo()
            codeinfo.cmdline_params = (
                [self.inputs.metadata.options.input_filename, ]
                + default_params + additional_params)
            codeinfo.code_uuid = code.uuid
            codeinfo.stdout_name = self.inputs.metadata.options.output_filename
            codeinfo.withmpi = False
            calcinfo.codes_info.append(codeinfo)

        return calcinfo
