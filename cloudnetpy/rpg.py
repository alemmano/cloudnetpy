"""This module contains RPG Cloud Radar related functions."""
import os
from collections import namedtuple
import numpy as np
import numpy.ma as ma


class Rpg:
    """RPG Cloud Radar Level 1 data reader."""
    def __init__(self, filename):
        self.filename = filename
        self._file_position = 0
        self.header = self.read_rpg_header()
        self.data = self.read_rpg_data()

    @staticmethod
    def read_string(file_id):
        """Read characters from binary data until whitespace."""
        str_out = ''
        while True:
            c = np.fromfile(file_id, np.int8, 1)
            if c:
                str_out += chr(c)
            else:
                break
        return str_out

    def read_rpg_header(self):
        """Reads the header or rpg binary file."""

        def append(names, dtype=np.int32, n_values=1):
            """Updates header dictionary."""
            for name in names:
                header[name] = np.fromfile(file, dtype, int(n_values))

        header = {}
        file = open(self.filename, 'rb')
        append(('file_code',
                'header_length'), np.int32)
        append(('_start_time',
                '_stop_time'), np.uint32)
        append(('program_number',))
        append(('model_number',))  # 0 = single polarization, 1 = dual pol.
        header['_program_name'] = Rpg.read_string(file)
        header['_customer_name'] = Rpg.read_string(file)
        append(('frequency',
                'antenna_separation',
                'antenna_diameter',
                'antenna_gain',  # linear
                'half_power_beam_width'), np.float32)
        append(('dual_polarization',), np.int8)  # 0 = single pol, 1 = dual pol (LDR), 2 = dual pol (STSR) ?
        append(('sample_duration',), np.float32)
        append(('latitude',
                'longitude'), np.float32)
        append(('calibration_interval_in_samples',
                'n_range_gates',
                'n_temperature_levels',
                'n_humidity_levels',
                'n_chirp_sequences'))
        append(('range',), np.float32, header['n_range_gates'])
        append(('temperature_levels',), np.float32, header['n_temperature_levels'])
        append(('humidity_levels',), np.float32, header['n_humidity_levels'])
        append(('n_spectral_samples_in_chirp',
                'chirp_start_indices',
                'n_averaged_chirps'), n_values=header['n_chirp_sequences'])
        append(('integration_time',
                'range_resolution',
                'nyquist_velocity'), np.float32, header['n_chirp_sequences'])
        append(('is_power_levelling',
                'is_spike_filter',
                'is_phase_correction',
                'is_relative_power_correction'), np.int8)
        append(('FFT_window',), np.int8)  # 0 = square, 1 = parzen, 2 = blackman, 3 = welch, = slepian2, 5 = slepian3
        append(('input_voltage',))
        append(('noise_filter_threshold_factor',), np.float32)
        self._file_position = file.tell()
        file.close()
        return header

    def read_rpg_data(self):
        """Reads the actual data from rpg binary file."""
        Dimensions = namedtuple('Dimensions', ['n_samples',
                                               'n_gates',
                                               'n_layers_t',
                                               'n_layers_h'])

        def _create_dimensions():
            """Returns possible lengths of the data arrays."""
            n_samples = np.fromfile(file, np.int32, 1)
            return Dimensions(int(n_samples),
                              int(self.header['n_range_gates']),
                              int(self.header['n_temperature_levels']),
                              int(self.header['n_humidity_levels']))

        def _create_variables():
            """Initializes dictionaries for data arrays."""
            vrs = {'sample_length': np.zeros(dims.n_samples, np.int),
                   'time': np.zeros(dims.n_samples, np.int),
                   'time_ms': np.zeros(dims.n_samples, np.int),
                   'quality_flag': np.zeros(dims.n_samples, np.int)}

            block1_vars = dict.fromkeys((
                'rain_rate',
                'relative_humidity',
                'temperature',
                'pressure',
                'wind_speed',
                'wind_direction',
                'voltage',
                'brightness_temperature',
                'liquid_water_path',
                'if_power',
                'elevation',
                'azimuth',
                'status_flag',
                'transmitted_power',
                'transmitter_temperature',
                'receiver_temperature',
                'pc_temperature'))

            block2_vars = dict.fromkeys((
                'reflectivity',
                'velocity',
                'width',
                'skewness',
                'kurtosis'))

            if self.header['dual_polarization']:
                block2_vars.update(dict.fromkeys((
                    'ldr',
                    'spectral_correlation_coefficient',
                    'differential_phase')))

            if self.header['dual_polarization'] == 2:
                block2_vars.update(dict.fromkeys((
                    '_',
                    'spectral_slanted_ldr',
                    'spectral_slanted_correlation_coefficient',
                    'specific_differential_phase_shift',
                    'differential_attenuation')))

            return vrs, block1_vars, block2_vars

        def _add_sensitivities():
            ind0 = len(block1) + n_dummy
            ind1 = ind0 + dims.n_gates
            block1['sensitivity_limit_v'] = float_block1[:, ind0:ind1]
            if self.header['dual_polarization']:
                block1['sensitivity_limit_h'] = float_block1[:, ind1:]

        def _get_length_of_dummy_data():
            return 3 + dims.n_layers_t + 2 * dims.n_layers_h

        def _get_length_of_sensitivity_data():
            if self.header['dual_polarization']:
                return 2*dims.n_gates
            return dims.n_gates

        def _get_float_block_lengths():
            block_one_length = len(block1) + n_dummy + n_sens
            block_two_length = len(block2)
            return block_one_length, block_two_length

        def _init_float_blocks():
            block_one = np.zeros((dims.n_samples, n_floats1))
            block_two = np.zeros((dims.n_samples, dims.n_gates, n_floats2))
            return block_one, block_two

        file = open(self.filename, 'rb')
        file.seek(self._file_position)
        dims = _create_dimensions()
        aux, block1, block2 = _create_variables()
        n_dummy = _get_length_of_dummy_data()
        n_sens = _get_length_of_sensitivity_data()
        n_floats1, n_floats2 = _get_float_block_lengths()
        float_block1, float_block2 = _init_float_blocks()

        for sample in range(dims.n_samples):
            aux['sample_length'][sample] = np.fromfile(file, np.int32, 1)
            aux['time'][sample] = np.fromfile(file, np.uint32, 1)
            aux['time_ms'][sample] = np.fromfile(file, np.int32, 1)
            aux['quality_flag'][sample] = np.fromfile(file, np.int8, 1)
            float_block1[sample, :] = np.fromfile(file, np.float32, n_floats1)
            is_data = np.fromfile(file, np.int8, dims.n_gates)
            is_data_ind = np.where(is_data)[0]
            n_valid = len(is_data_ind)
            values = np.fromfile(file, np.float32, n_floats2*n_valid)
            float_block2[sample, is_data_ind, :] = values.reshape(n_valid, n_floats2)
        file.close()
        for n, name in enumerate(block1):
            block1[name] = float_block1[:, n]
        _add_sensitivities()
        for n, name in enumerate(block2):
            block2[name] = float_block2[:, :, n]
        return {**aux, **block1, **block2}


def get_rpg_files(path_to_l1_files):
    """Returns list of RPG Level 1 files for one day - sorted by filename."""
    files = os.listdir(path_to_l1_files)
    l1_files = [f"{path_to_l1_files}{file}" for file in files if file.endswith('LV1')]
    l1_files.sort()
    return l1_files


def get_rpg_objects(rpg_files):
    """Creates a list of Rpg() objects from the filenames."""
    for file in rpg_files:
        yield Rpg(file)


def _stack_rpg_data(rpg_objects):
    """Combines selected data from hourly Rpg() objects.

    Notes:
        Concatenate is slow (?) because we don't have the size
        of the problem beforehand.. maybe try to fix this.

    """
    def _stack(source, target, fun):
        for name, value in source.items():
            if not name.startswith('_'):
                target[name] = (fun((target[name], value))
                                if name in target else value)

    data = {}
    header = {}
    for rpg in rpg_objects:
        _stack(rpg.data, data, np.concatenate)
        _stack(rpg.header, header, np.vstack)
    return data, header


def _reduce_header(header):
    """Removes duplicate header data."""
    for name in header:
        first_row = header[name][0]
        assert np.isclose(header[name], first_row).all(), f"Inconsistent header: {name}"
        header[name] = first_row
    return header


def _mask_invalid_data(rpg_data):
    for name in rpg_data:
        rpg_data[name] = ma.masked_equal(rpg_data[name], 0)
    return rpg_data


def rpg2nc(path_to_l1_files, output_file):
    l1_files = get_rpg_files(path_to_l1_files)
    rpg_objects = get_rpg_objects(l1_files)
    rpg_data, rpg_header = _stack_rpg_data(rpg_objects)
    rpg_header = _reduce_header(rpg_header)
    rpg_data = _mask_invalid_data(rpg_data)
    return {**rpg_header, **rpg_data}

