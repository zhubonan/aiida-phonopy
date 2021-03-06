from aiida.engine import WorkChain
from aiida.plugins import DataFactory
from aiida.orm import Float, Bool, Str, Code
from aiida.engine import if_
from aiida_phonopy.common.generate_inputs import (get_calcjob_builder,
                                                  get_immigrant_builder)
from aiida_phonopy.common.utils import (
    get_force_sets, get_force_constants, get_nac_params, get_phonon,
    get_phonon_setting_info, check_imported_supercell_structure,
    from_node_id_to_aiida_node_id, get_data_from_node_id)


# Should be improved by some kind of WorkChainFactory
# For now all workchains should be copied to aiida/workflows

Dict = DataFactory('dict')
ArrayData = DataFactory('array')
XyData = DataFactory('array.xy')
StructureData = DataFactory('structure')
BandsData = DataFactory('array.bands')


class PhonopyWorkChain(WorkChain):
    """ Workchain to do a phonon calculation using phonopy

    inputs
    ------
    structure : StructureData
        Unit cell structure.
    calculator_settings : Dict
        Settings to run force and nac calculations. For example,
            {'forces': force_config,
             'nac': nac_config}
        At least 'forces' key is necessary. 'nac' is optional.
        force_config is used for supercell force calculation. nac_config
        are used for Born effective charges and dielectric constant calculation
        in primitive cell. The primitive cell is chosen by phonopy
        automatically.
    phonon_settings : Dict
        Setting to run phonon calculation. Keys are:
        supercell_matrix : list or list of list
            Multiplicity to create supercell from unit cell. Three integer
            values (list) or 3x3 integer values (list of list).
        mesh : list of float, optional
            List of three integer values or float to represent distance between
            neighboring q-points. Default is 100.0.
        distance : float, optional
            Atomic displacement distance. Default is 0.01.
        is_nac : bool, optional
            Whether running non-analytical term correction or not. Default is
            False.
        displacement_dataset : dict
            Atomic displacement dataset that phonopy can understand.
    run_phonopy : Bool, optional
        Whether running phonon calculation or not. Default is False.
    remote_phonopy : Bool, optional
        Whether running phonon calculation or not at remote. Default is False.
    code_string : Str, optional
        Code string of phonopy needed when both of run_phonopy and
        remote_phonopy are True.
    options : Dict
        AiiDa calculation options for phonon calculation used when both of
        run_phonopy and remote_phonopy are True.
    symmetry_tolerance : Float, optional
        Symmetry tolerance. Default is 1e-5.
    immigrant_calculation_folders : Dict, optional
        'force' key has to exist and 'nac' is necessary when
        phonon_settings['is_nac'] is True. The value of the 'force' key is
        the list of strings of remote directories. The value of 'nac' is the
        string of remote directory.
    calculation_nodes : Dict, optional
        This works similarly as immigrant_calculation_folders but contains
        PK or UUID instead of string of remote folder.

    """

    @classmethod
    def define(cls, spec):
        super(PhonopyWorkChain, cls).define(spec)
        spec.input('structure', valid_type=StructureData, required=True)
        spec.input('phonon_settings', valid_type=Dict, required=True)
        spec.input('displacement_dataset', valid_type=Dict, required=False)
        spec.input('immigrant_calculation_folders',
                   valid_type=Dict, required=False)
        spec.input('calculation_nodes',
                   valid_type=Dict, required=False)
        spec.input('calculator_settings', valid_type=Dict, required=False)
        spec.input('code_string', valid_type=Str, required=False)
        spec.input('options', valid_type=Dict, required=False)
        spec.input('symmetry_tolerance',
                   valid_type=Float, required=False, default=Float(1e-5))
        spec.input('dry_run',
                   valid_type=Bool, required=False, default=Bool(False))
        spec.input('run_phonopy',
                   valid_type=Bool, required=False, default=Bool(False))
        spec.input('remote_phonopy',
                   valid_type=Bool, required=False, default=Bool(False))

        spec.outline(
            cls.initialize_supercell_phonon_calculation,
            if_(cls.import_calculations)(
                if_(cls.import_calculations_from_files)(
                    cls.read_force_and_nac_calculations_from_files,
                ),
                if_(cls.import_calculations_from_nodes)(
                    cls.read_calculation_data_from_nodes,
                ),
                cls.check_imported_supercell_structures,
            ).else_(
                cls.run_force_and_nac_calculations,
            ),
            if_(cls.dry_run)(
                cls.postprocess_of_dry_run,
            ).else_(
                cls.create_force_sets,
                if_(cls.is_nac)(cls.create_nac_params),
                if_(cls.run_phonopy)(
                    if_(cls.remote_phonopy)(
                        cls.run_phonopy_remote,
                        cls.collect_data,
                    ).else_(
                        cls.create_force_constants,
                        cls.run_phonopy_in_workchain,
                    )
                )
            )
        )
        spec.output('force_constants', valid_type=ArrayData, required=False)
        spec.output('primitive', valid_type=StructureData, required=False)
        spec.output('supercell', valid_type=StructureData, required=False)
        spec.output('force_sets', valid_type=ArrayData, required=False)
        spec.output('nac_params', valid_type=ArrayData, required=False)
        spec.output('thermal_properties', valid_type=XyData, required=False)
        spec.output('band_structure', valid_type=BandsData, required=False)
        spec.output('dos', valid_type=XyData, required=False)
        spec.output('pdos', valid_type=XyData, required=False)
        spec.output('phonon_setting_info', valid_type=Dict, required=True)

    def dry_run(self):
        return self.inputs.dry_run

    def remote_phonopy(self):
        return self.inputs.remote_phonopy

    def run_phonopy(self):
        return self.inputs.run_phonopy

    def is_nac(self):
        if 'is_nac' in self.inputs.phonon_settings.attributes:
            return self.inputs.phonon_settings['is_nac']
        else:
            False

    def import_calculations_from_files(self):
        return 'immigrant_calculation_folders' in self.inputs

    def import_calculations_from_nodes(self):
        return 'calculation_nodes' in self.inputs

    def import_calculations(self):
        if 'immigrant_calculation_folders' in self.inputs:
            return True
        if 'calculation_nodes' in self.inputs:
            return True
        return False

    def initialize_supercell_phonon_calculation(self):
        """Set default settings and create supercells and primitive cell"""

        self.report('initialize_supercell_phonon_calculation')

        if self.inputs.run_phonopy and self.inputs.remote_phonopy:
            if ('code_string' not in self.inputs or
                'options' not in self.inputs):
                raise RuntimeError(
                    "code_string and options have to be specified.")

        if 'supercell_matrix' not in self.inputs.phonon_settings.attributes:
            raise RuntimeError(
                "supercell_matrix was not found in phonon_settings.")

        if 'displacement_dataset' in self.inputs:
            return_vals = get_phonon_setting_info(
                self.inputs.phonon_settings,
                self.inputs.structure,
                self.inputs.symmetry_tolerance,
                displacement_dataset=self.inputs.displacement_dataset)
        else:
            return_vals = get_phonon_setting_info(
                self.inputs.phonon_settings,
                self.inputs.structure,
                self.inputs.symmetry_tolerance)
        self.ctx.phonon_setting_info = return_vals['phonon_setting_info']
        self.out('phonon_setting_info', self.ctx.phonon_setting_info)

        self.ctx.supercells = {}
        for i in range(len(return_vals) - 3):
            label = "supercell_%03d" % (i + 1)
            self.ctx.supercells[label] = return_vals[label]
        self.ctx.primitive = return_vals['primitive']
        self.ctx.supercell = return_vals['supercell']
        self.out('primitive', self.ctx.primitive)
        self.out('supercell', self.ctx.supercell)

    def run_force_and_nac_calculations(self):
        self.report('run force calculations')

        # Forces
        for i in range(len(self.ctx.supercells)):
            label = "supercell_%03d" % (i + 1)
            builder = get_calcjob_builder(self.ctx.supercells[label],
                                          self.inputs.calculator_settings,
                                          calc_type='forces',
                                          label=label)
            future = self.submit(builder)
            self.report('{} pk = {}'.format(label, future.pk))
            self.to_context(**{label: future})

        # Born charges and dielectric constant
        if self.ctx.phonon_setting_info['is_nac']:
            self.report('calculate born charges and dielectric constant')
            builder = get_calcjob_builder(self.ctx.primitive,
                                          self.inputs.calculator_settings,
                                          calc_type='nac',
                                          label='born_and_epsilon')
            future = self.submit(builder)
            self.report('born_and_epsilon: {}'.format(future.pk))
            self.to_context(**{'born_and_epsilon': future})

    def read_force_and_nac_calculations_from_files(self):
        self.report('import calculation data in files')

        calc_folders_Dict = self.inputs.immigrant_calculation_folders
        for i, force_folder in enumerate(calc_folders_Dict['force']):
            label = "supercell_%03d" % (i + 1)
            builder = get_immigrant_builder(force_folder,
                                            self.inputs.calculator_settings,
                                            calc_type='forces')
            builder.metadata.label = label
            future = self.submit(builder)
            self.report('{} pk = {}'.format(label, future.pk))
            self.to_context(**{label: future})

        if self.ctx.phonon_setting_info['is_nac']:  # NAC the last one
            label = 'born_and_epsilon'
            builder = get_immigrant_builder(calc_folders_Dict['nac'][0],
                                            self.inputs.calculator_settings,
                                            calc_type='nac')
            builder.metadata.label = label
            future = self.submit(builder)
            self.report('{} pk = {}'.format(label, future.pk))
            self.to_context(**{label: future})

    def read_calculation_data_from_nodes(self):
        self.report('import calculation data from nodes')

        calc_nodes_Dict = self.inputs.calculation_nodes

        for i, node_id in enumerate(calc_nodes_Dict['force']):
            label = "supercell_%03d" % (i + 1)
            aiida_node_id = from_node_id_to_aiida_node_id(node_id)
            # self.ctx[label]['forces'] -> ArrayData()('final')
            self.ctx[label] = get_data_from_node_id(aiida_node_id)

        if self.ctx.phonon_setting_info['is_nac']:  # NAC the last one
            label = 'born_and_epsilon'
            node_id = calc_nodes_Dict['nac'][0]
            aiida_node_id = from_node_id_to_aiida_node_id(node_id)
            # self.ctx[label]['born_charges'] -> ArrayData()('born_charges')
            # self.ctx[label]['dielectrics'] -> ArrayData()('epsilon')
            self.ctx[label] = get_data_from_node_id(aiida_node_id)

    def check_imported_supercell_structures(self):
        self.report('check imported supercell structures')

        msg = ("Immigrant failed because of inconsistency of supercell"
               "structure")

        for i in range(len(self.ctx.supercells)):
            label = "supercell_%03d" % (i + 1)
            calc = self.ctx[label]
            if type(calc) is dict:
                calc_dict = calc
            else:
                calc_dict = calc.inputs
            supercell_ref = self.ctx.supercells[label]
            supercell_calc = calc_dict['structure']
            if not check_imported_supercell_structure(
                    supercell_ref,
                    supercell_calc,
                    self.inputs.symmetry_tolerance):
                raise RuntimeError(msg)

    def postprocess_of_dry_run(self):
        self.report('Finish here because of dry-run setting')

    def create_force_sets(self):
        """Build datasets from forces of supercells with displacments"""

        self.report('create force sets')

        # VASP specific
        forces_dict = {}

        for i in range(len(self.ctx.supercells)):
            label = "supercell_%03d" % (i + 1)
            calc = self.ctx[label]
            if type(calc) is dict:
                calc_dict = calc
            else:
                calc_dict = calc.outputs
            if ('forces' in calc_dict and
                'final' in calc_dict['forces'].get_arraynames()):
                label = "forces_%03d" % (i + 1)
                forces_dict[label] = calc_dict['forces']
            else:
                msg = ("Forces could not be found in calculation %03d."
                       % (i + 1))
                self.report(msg)

            if ('misc' in calc_dict and
                'total_energies' in calc_dict['misc'].keys()):
                label = "misc_%03d" % (i + 1)
                forces_dict[label] = calc_dict['misc']

        if sum(['forces' in k for k in forces_dict]) != len(self.ctx.supercells):
            raise RuntimeError("Forces could not be retrieved.")

        self.ctx.force_sets = get_force_sets(**forces_dict)
        self.out('force_sets', self.ctx.force_sets)

    def create_nac_params(self):
        self.report('create nac data')

        # VASP specific
        # Call workfunction to make links
        calc = self.ctx.born_and_epsilon
        if type(calc) is dict:
            calc_dict = calc
            structure = calc['structure']
        else:
            calc_dict = calc.outputs
            structure = calc.inputs.structure

        if 'born_charges' not in calc_dict:
            raise RuntimeError(
                "Born effective charges could not be found "
                "in the calculation. Please check the calculation setting.")
        if 'dielectrics' not in calc_dict:
            raise RuntimeError(
                "Dielectric constant could not be found "
                "in the calculation. Please check the calculation setting.")

        params = {'symmetry_tolerance':
                  Float(self.ctx.phonon_setting_info['symmetry_tolerance'])}
        if self.import_calculations():
            params['primitive'] = self.ctx.primitive
        self.ctx.nac_params = get_nac_params(
            calc_dict['born_charges'],
            calc_dict['dielectrics'],
            structure,
            **params)
        self.out('nac_params', self.ctx.nac_params)

    def run_phonopy_remote(self):
        """Run phonopy at remote computer"""

        self.report('remote phonopy calculation')

        code_string = self.inputs.code_string.value
        builder = Code.get_from_string(code_string).get_builder()
        builder.structure = self.inputs.structure
        builder.settings = self.ctx.phonon_setting_info
        builder.metadata.options.update(self.inputs.options)
        builder.metadata.label = self.inputs.metadata.label
        builder.force_sets = self.ctx.force_sets
        if 'nac_params' in self.ctx:
            builder.nac_params = self.ctx.nac_params
            builder.primitive = self.ctx.primitive
        future = self.submit(builder)

        self.report('phonopy calculation: {}'.format(future.pk))
        self.to_context(**{'phonon_properties': future})
        # return ToContext(phonon_properties=future)

    def collect_data(self):
        self.report('collect data')
        self.out('thermal_properties',
                 self.ctx.phonon_properties.outputs.thermal_properties)
        self.out('dos', self.ctx.phonon_properties.outputs.dos)
        self.out('pdos', self.ctx.phonon_properties.outputs.pdos)
        self.out('band_structure',
                 self.ctx.phonon_properties.outputs.band_structure)
        self.out('force_constants',
                 self.ctx.phonon_properties.outputs.force_constants)

        self.report('finish phonon')

    def create_force_constants(self):
        self.report('create force constants')

        self.ctx.force_constants = get_force_constants(
            self.inputs.structure,
            self.ctx.phonon_setting_info,
            self.ctx.force_sets)
        self.out('force_constants', self.ctx.force_constants)

    def run_phonopy_in_workchain(self):
        self.report('phonopy calculation in workchain')

        params = {}
        if 'nac_params' in self.ctx:
            params['nac_params'] = self.ctx.nac_params
        result = get_phonon(self.inputs.structure,
                            self.ctx.phonon_setting_info,
                            self.ctx.force_constants,
                            **params)
        self.out('thermal_properties', result['thermal_properties'])
        self.out('dos', result['dos'])
        self.out('band_structure', result['band_structure'])

        self.report('finish phonon')
