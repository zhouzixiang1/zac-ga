import itertools
import math

import numpy as np


class HyperParamSets(object):
    def __init__(self, configs) -> None:
        self.all_sets = []
        self.configs = configs

        # these parameters changes together
        binding_params = configs.backend_params.get("binding_params", [])

        binding_changes_dict = {}

        retranspile_changes = configs.backend_params.retranspile_changes
        nonretranspile_changes = configs.backend_params.nonretranspile_changes
        retranspile_binding = False

        for binding_param in binding_params:
            if binding_param in retranspile_changes.keys():
                retranspile_binding = True
                binding_changes_dict[binding_param] = retranspile_changes[binding_param]
                binding_len = len(retranspile_changes[binding_param])
            elif binding_param in nonretranspile_changes.keys():
                binding_changes_dict[binding_param] = nonretranspile_changes[
                    binding_param
                ]
                binding_len = len(nonretranspile_changes[binding_param])
            else:
                raise ValueError(
                    f"binding_param {binding_param} not in retranspile_changes or nonretranspile_changes"
                )

            lens = map(len, binding_changes_dict.values())
            assert len(set(lens)) == 1

        if binding_params is not None:
            # remove binding parameters from retranspile_changes and nonretranspile_changes
            for key in binding_params:
                if key in retranspile_changes.keys():
                    retranspile_changes.pop(key)
                if key in nonretranspile_changes.keys():
                    nonretranspile_changes.pop(key)

        retranspile_period = math.prod(
            [len(nonretranspile_changes[key]) for key in nonretranspile_changes.keys()]
        )

        if not retranspile_binding and len(binding_params) > 0:
            retranspile_period *= binding_len

        all_keys = list(retranspile_changes.keys()) + list(
            nonretranspile_changes.keys()
        )
        all_products = list(
            itertools.product(
                *(
                    list(retranspile_changes.values())
                    + list(nonretranspile_changes.values())
                )
            )
        )
        all_dicts = []
        for product in all_products:
            all_dicts.append(dict(zip(all_keys, product)))

        all_dicts_new = []
        if len(binding_params) > 1:
            if all_dicts == []:
                for k in binding_len:
                    for binding_param in binding_params:
                        all_dicts.append(
                            {binding_param: binding_changes_dict[binding_param][k]}
                        )

            else:
                for dic in all_dicts:
                    for k in range(binding_len):
                        for binding_param in binding_params:
                            dic[binding_param] = binding_changes_dict[binding_param][k]
                        all_dicts_new.append(dic.copy())

                all_dicts = all_dicts_new

        for k, dic in enumerate(all_dicts):
            hyperparams = HyperParams(
                base_set=configs.backend_params.base_set, preset_params=dic
            )
            # for i, key in enumerate(all_keys):
            # hyperparams.__dict__[key] = product[i]
            if k % retranspile_period == 0:
                hyperparams.retranspile = True

            hyperparams.configs = configs.dict()
            self.all_sets.append(hyperparams)
        if not (configs.compiler.name == "fpqac_generic"):
            for i in range(len(self.all_sets)):
                self.all_sets[i].n_rows = self.all_sets[i].n_rows[0].item()
                self.all_sets[i].n_cols = self.all_sets[i].n_cols[0].item()
                self.all_sets[i].fpqac_generic_n_cols_cmap = (
                    self.all_sets[i].fpqac_generic_n_cols_cmap[0].item()
                )
                self.all_sets[i].fpqac_generic_n_rows_cmap = (
                    self.all_sets[i].fpqac_generic_n_rows_cmap[0].item()
                )
                self.all_sets[i].n_atoms_per_array = (
                    self.all_sets[i].n_atoms_per_array[0].item()
                )

    def __repr__(self) -> str:
        return self.all_sets.__repr__()

    def __getitem__(self, key):
        if isinstance(self.all_sets[key], str):
            try:
                res = eval(self.all_sets[key])
            except:
                res = self.all_sets[key]
            return res
        else:
            return self.all_sets[key]


class HyperParams(object):
    def __init__(self, base_set, preset_params=None):
        self.base_set = base_set
        self.retranspile = False
        if base_set == "default0":
            self.get_default0_params(**preset_params)
        elif base_set == "default1":
            pass

    def get_default0_params(
        self,
        n_rows=[10, 10, 10],
        n_cols=[10, 10, 10],
        n_atoms_per_array=None,
        n_aods=1,
        atom_distance=15 * 1e-6,
        T_per_move=300 * 1e-6,
        x_zpf=38 * 1e-9,
        omega_0=2 * np.pi * 80 * 1e3,
        na_T1=1.5 * 10,
        na_N_max=33,
        na_1Q_time=1 / 3.2e6 * 4 / 2,
        na_2Q_time=190 * 1e-9 * 2,
        # na_n_move_per_cooling=20,
        cooling_deltaN_thres=15,
        na_1Q_fidelity=0.99992,
        na_2Q_fidelity=0.9975,
        na_simultaneous_1q_gate=1e10,
        lambd=0.109375,
        atom_loss_prob=0.0068,
        atom_transfer_time=15 * 1e-6,
        sc_T1=None,
        sc_T2=80.12 * 1e-6 * 10,
        sc_1Q_time=160 * 0.22 * 1e-9,
        sc_2Q_time=480 * 1e-9,
        # sc_1Q_fidelity=0.99992,
        # sc_2Q_fidelity=0.9995,
        sc_simultaneous_1q_gate=1e10,
        sc_seed_transpiler=0,
        fpqac_generic_cmap_bidirect=True,
        fpqac_generic_seed_transpiler=0,
        fpqac_generic_n_rows_cmap=10,
        fpqac_generic_n_cols_cmap=10,
        fpqac_qubit_array_mapper="default",
        fpqac_qubit_atom_mapper="default",
        fpqac_router="default",
        geyser_use_blocking=False,
        geyser_dump_transpiled_qasm=True,
        geyser_generic_seed_transpiler=5,
        faa_cmap_bidirect=True,
        faa_seed_transpiler=0,
        flying_search_method="DP",
        flying_square_mapping=True,
        flying_QSIM_max_ancilla=9999,
        flying_1gate_per_stage_for_QAOA=False,
        full_report_path=None,
        dump_full_report=False,
        log_path=None,
        # fpqac_generic_partition_method="SDP",
        fpqac_generic_partition_method="heuristic",
        fpqac_generic_partition_factor=0.9,
        backer_penalty=2,
        fpqac_individual_addressable=False,  # constraint 1
        fpqac_allow_order_violation=False,  # constraint 2
        fpqac_allow_overlap_violation=False,  # constraint 3
    ):
        ################# atom array settings:
        self.n_rows = np.array(n_rows)
        self.n_cols = np.array(n_cols)
        self.log_path = log_path

        if n_atoms_per_array is None:
            self.n_atoms_per_array = self.n_rows * self.n_cols
        else:
            # when not all atoms in the array can be used. For example, 95 atoms in a 10X10 array
            self.n_atoms_per_array = n_atoms_per_array
        self.n_aods = n_aods
        self.flying_search_method = flying_search_method
        self.flying_square_mapping = flying_square_mapping
        self.flying_QSIM_max_ancilla = flying_QSIM_max_ancilla
        self.flying_1gate_per_stage_for_QAOA = flying_1gate_per_stage_for_QAOA
        self.full_report_path = full_report_path
        # source: A quantum processor based on coherent transport of entangled atom arrays
        self.atom_distance = atom_distance
        self.T_per_move = T_per_move
        self.x_zpf = x_zpf
        self.omega_0 = omega_0
        # self.move_speed = 0.55  # 0.55u/us,  15u * 9 135 u 270us
        # maybe 150KHz
        # source Quantum logic and entanglement by neutral Rydberg atoms: methods and fidelity
        # Madjarov I S et al 2020 High-fidelity entanglement and detection of alkaline-earth Rydberg atoms Nat. Phys. 16 857
        # Madjarov I S et al 2021 High-fidelity entanglement and detection of alkaline-earth Rydberg atoms Nat. Phys. 17 144
        self.na_T1 = na_T1
        # self.na_T2 = self.na_T1
        self.na_N_max = na_N_max  # Maximal heating before the atom is lost
        # source: Olmschenk S, Chicireanu R, Nelson K D and Porto J V 2010 Randomized benchmarking of atomic qubits in an optical lattice
        # New J. Phys. 12 113007
        self.na_1Q_time = na_1Q_time
        # source: A quantum processor based on coherent transport of entangled atom arrays
        self.na_2Q_time = na_2Q_time

        self.cooling_deltaN_thres = cooling_deltaN_thres
        # self.na_n_move_per_cooling = na_n_move_per_cooling
        # source: Geyser
        # Gerard Pelegrí, Andrew J Daley, and Jonathan D Pritchard. 2021. High-Fidelity
        # Multiqubit Rydberg Gates via Two-Photon Adiabatic Rapid Passage. arXiv
        # preprint arXiv:2112.13025 (2021).
        # M Saffman, II Beterov, A Dalal, EJ Páez, and BC Sanders. 2020. Symmetric
        # Rydberg Controlled-Z Gates with Adiabatic Pulses
        # T Xia, M Lichtman, K Maller, A W Carr, M J Piotrowicz, L Isenhower, and M
        # Saffman. 2015. Randomized Benchmarking of Single-qubit Gates in a 2D Array
        # of Neutral-atom Qubits. Phys. Rev. Lett. 114, 10 (March 2015), 100503.
        self.na_1Q_fidelity = na_1Q_fidelity
        self.na_2Q_fidelity = na_2Q_fidelity

        # long range is for Baker paper, the error rate is three times higher
        self.na_2Q_fidelity_long_range = 1 - (1 - na_2Q_fidelity) * (backer_penalty + 1)

        self.na_simultaneous_1q_gate = na_simultaneous_1q_gate

        f2 = na_2Q_fidelity
        self.lambd = lambd  # see the computation in lambda_compute.py
        self.two_q_error_delta_N = (1 - f2) * self.lambd

        # J. P. Covey, I. S. Madjarov, A. Cooper, and M. Endres, “2000-times
        # repeated imaging of strontium atoms in clock-magic tweezer arrays,”
        # Phys. Rev. Lett., vol. 122, p. 173201, May 2019. [Online]. Available:
        # https://link.aps.org/doi/10.1103/PhysRevLett.122.173201
        self.atom_loss_prob = atom_loss_prob

        # Quantum phases of matter on a 256-atom programmable quantum simulator
        # Sepehr Ebadi, Tout T. Wang, Harry Levine, Alexander Keesling, Giulia Semeghini, Ahmed Omran, Dolev Bluvstein, Rhine Samajdar, Hannes Pichler, Wen Wei Ho, Soonwon Choi, Subir Sachdev, Markus Greiner, Vladan Vuletić & Mikhail D. Lukin
        self.atom_transfer_time = atom_transfer_time

        ################ superconducting settings
        self.sc_T1 = sc_T1
        self.sc_T2 = sc_T2

        self.sc_1Q_time = sc_1Q_time
        self.sc_2Q_time = sc_2Q_time

        self.sc_1Q_fidelity = na_1Q_fidelity
        self.sc_2Q_fidelity = na_2Q_fidelity

        self.sc_simultaneous_1q_gate = sc_simultaneous_1q_gate
        self.sc_seed_transpiler = sc_seed_transpiler

        ################# FPQAC generic
        self.fpqac_generic_cmap_bidirect = fpqac_generic_cmap_bidirect
        self.fpqac_generic_seed_transpiler = fpqac_generic_seed_transpiler
        self.fpqac_generic_n_rows_cmap = self.n_rows
        self.fpqac_generic_n_cols_cmap = self.n_cols
        self.fpqac_qubit_array_mapper = fpqac_qubit_array_mapper
        self.fpqac_qubit_atom_mapper = fpqac_qubit_atom_mapper
        self.fpqac_router = fpqac_router
        self.fpqac_individual_addressable = fpqac_individual_addressable
        self.fpqac_allow_order_violation = fpqac_allow_order_violation
        self.fpqac_allow_overlap_violation = fpqac_allow_overlap_violation
        ################# Geyser
        self.geyser_use_blocking = geyser_use_blocking
        self.geyser_dump_transpiled_qasm = geyser_dump_transpiled_qasm
        self.geyser_generic_seed_transpiler = geyser_generic_seed_transpiler

        ################# FAA
        self.faa_cmap_bidirect = faa_cmap_bidirect
        self.faa_seed_transpiler = faa_seed_transpiler

        self.dump_full_report = dump_full_report
        self.fpqac_generic_partition_method = fpqac_generic_partition_method
        self.fpqac_generic_partition_factor = fpqac_generic_partition_factor

    # def __getitem__(self, key):
    # return self.__dict__[key]
    def __getitem__(self, key):
        if isinstance(self.__dict__[key], str):
            try:
                res = eval(self.__dict__[key])
            except:
                res = self.__dict__[key]
            return res
        else:
            return self.__dict__[key]

    def __repr__(self) -> str:
        return self.__dict__.__repr__()
