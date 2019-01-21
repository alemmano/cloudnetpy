""" Functions for rebinning input data.
"""
import os
import sys
sys.path.insert(0, os.path.abspath('../../cloudnetpy'))
import math
import numpy as np
import numpy.ma as ma
import scipy.constants
from scipy.interpolate import interp1d
import netCDF4
from cloudnetpy import config
from cloudnetpy import utils
from cloudnetpy import atmos
from cloudnetpy import classify
from cloudnetpy import output
from cloudnetpy.cloudnetarray import CloudnetArray


class RawDataSource():
    """Base class for all Cloudnet measurements and model data.

    Args:
        input_file (str): Calibrated instrument / model NetCDF file.

    Attributes:
        filename (str): Filename of the input file.
        dataset (Dataset): A netcdf4 Dataset instance.
        variables (dict): Variables of the Dataset instance.
        source (str): Global attribute 'source' from *input_file*.
        time (MaskedArray): Time array of the instrument.
        data (dict): Dictionary containing CloudnetArray instances.

    """
    def __init__(self, input_file):
        self.filename = input_file
        self.dataset = netCDF4.Dataset(self.filename)
        self.variables = self.dataset.variables
        self.source = self._get_global_attribute('source')
        self.time = self._getvar('time')
        self.data = {}

    def _fix_time(self):
        if max(self.time) > 24:
            self.time = utils.seconds2hour(self.time)

    def _get_altitude(self):
        """Returns altitude of the instrument (m)."""
        return float(utils.km2m(self.variables['altitude']))

    def _get_height(self):
        """Returns height array above mean sea level (m)."""
        range_instrument = utils.km2m(self.variables['range'])
        alt_instrument = self._get_altitude()
        return np.array(range_instrument + alt_instrument)

    def _get_global_attribute(self, attr_name):
        """Returns attribute from the source file."""
        if hasattr(self.dataset, attr_name):
            return getattr(self.dataset, attr_name)
        return ''

    def _getvar(self, *args):
        """Returns data (without attributes) from the source file."""
        for arg in args:
            if arg in self.variables:
                return self.variables[arg][:]
        raise KeyError('Missing variable')

    def _append_data(self, data, key, name=None, units=None):
        self.data[key] = CloudnetArray(data, name or key, units)

    def netcdf_to_cloudnet(self, fields):
        """Transforms netCDF4-variables into CloudnetArrays.

        Args:
            fields (tuple): netCDF4-variables to be converted. The results are
                saved in *self.data* dictionary with *fields* strings as keys.

        """
        for key in fields:
            self._append_data(self.variables[key], key)


class Radar(RawDataSource):
    """Radar class, child of RawDataSource.

    Args:
        radar_file (str): File name of the calibrated radar file.
        fields (tuple): Tuple of strings containing fields to be extracted
            from the *radar_file*, e.g ('Zh', 'ldr', 'v', 'width').

    Attributes:
        radar_frequency (float): Radar frequency (GHz).
        wl_band (int): Int corresponding to frequency 0 = 35.5 GHz, 1 = 94 GHz.
        folding_velocity (float): Radar's folding velocity (m/s).
        altitude (float): Altitude of the radar above mean sea level (m).
        height (ndarray): Measurement height grid above mean sea level (m).
        location (str): Location of the radar, copied from the global attribute
            'location' of the *radar_file*.

    """
    def __init__(self, radar_file, fields):
        super().__init__(radar_file)
        self.radar_frequency = float(self._getvar('radar_frequency',
                                                  'frequency'))
        self.wl_band = self._get_wl_band()
        self.folding_velocity = self._get_folding_velocity()
        self.altitude = self._get_altitude()
        self.height = self._get_height()
        self.location = self._get_global_attribute('location')
        self.netcdf_to_cloudnet(fields)

    def _get_wl_band(self):
        return 0 if (30 < self.radar_frequency < 40) else 1

    def _get_folding_velocity(self):
        if 'NyquistVelocity' in self.variables:
            return float(self._getvar('NyquistVelocity'))
        elif 'prf' in self.variables:
            return float(self._getvar('prf') * scipy.constants.c
                         / (4 * self.radar_frequency * 1e9))
        else:
            raise KeyError('Unable to determine folding velocity')

    def rebin_data(self, time_new):
        """Rebins radar data in time using mean.

        Args:
            time_new (ndarray): Target time array as fraction hour. Updates
            *self.time* attribute.

        """
        for key in self.data:
            if key in ('Zh',):
                self.data[key].db2lin()
                self.data[key].rebin_data(self.time, time_new)
                self.data[key].lin2db()
            elif key in ('v',):
                self.data[key].rebin_in_polar(self.time, time_new,
                                              self.folding_velocity)
            else:
                self.data[key].rebin_data(self.time, time_new)
        self.time = time_new

    def correct_atten(self, gas_atten, liq_atten):
        """Corrects radar echo for liquid and gas attenuation.

        Args:
            gas_atten (ndarray): 2-D attenuation due to atmospheric gases.
            liq_atten (ndarray): 2-D attenuation due to liquid water.

        """
        z_corrected = self.data['Zh'][:]
        z_corrected += gas_atten
        ind = ~liq_atten.mask
        z_corrected[ind] += liq_atten[ind]
        self._append_data(z_corrected, 'Z')

    def calc_errors(self, gas_atten, liq_atten_err, is_clutter,
                    is_uncorrected_liquid):
        """Calculates error and sensitivity of radar echo.

        Args:
            gas_atten (ndarray): 2-D attenuation due to atmospheric gases.
            liq_atten (ndarray): 2-D attenuation due to atmospheric liquid.
            liq_atten_err (ndarray): Error in liquid attenuation.
            is_clutter (ndarray): Boolean array of clutter presence.
            is_uncorrected_liquid (ndarray): Boolean array denoting where liquid
                attenuation is not corrected.

        """
        z = self.data['Zh'][:]
        radar_range = utils.km2m(self.variables['range'])
        log_range = utils.lin2db(radar_range, scale=20)
        z_power = z - log_range
        z_power_min = np.percentile(z_power.compressed(), 0.1)
        z_sensitivity = z_power_min + log_range + ma.mean(gas_atten, axis=0)
        zc = ma.median(ma.array(z, mask=~is_clutter), axis=0)
        z_sensitivity[~zc.mask] = zc[~zc.mask]
        dwell_time = utils.mdiff(self.time) * 3600  # seconds
        independent_pulses = (dwell_time * self.radar_frequency * 1e9 * 4
                              * np.sqrt(math.pi) * self.data['width'][:] / 3e8)
        z_precision = 4.343 * (1 / np.sqrt(independent_pulses)
                               + utils.db2lin(z_power_min - z_power) / 3)
        z_error = utils.l2norm(gas_atten * config.GAS_ATTEN_PREC, liq_atten_err,
                               z_precision)
        z_error[is_uncorrected_liquid] = ma.masked
        self._append_data(z_error, 'Z_error')
        self._append_data(z_sensitivity, 'Z_sensitivity')
        self._append_data(config.Z_BIAS, 'Z_bias')

    def add_meta(self):
        """Copies misc. metadata from the input file."""
        for key in ('latitude', 'longitude', 'altitude'):
            self._append_data(self._getvar(key), key)
        for key in ('time', 'height', 'radar_frequency'):
            self._append_data(getattr(self, key), key)


class Lidar(RawDataSource):
    """Lidar class, child of RawDataSource.

    Args:
        lidar_file (str): File name of the calibrated lidar file.
        fields (tuple): Tuple of strings containing fields to be extracted
            from the *lidar_file*, e.g ('beta', 'beta_raw').

    Attributes:
        height (ndarray): Measurement height grid above mean sea level (m).

    """
    def __init__(self, lidar_file, fields):
        super().__init__(lidar_file)
        self.height = self._get_height()
        self.netcdf_to_cloudnet(fields)

    def rebin_data(self, time_new, height_new):
        """Rebins lidar data in time and height using mean.

        Args:
            time_new (ndarray): 1-D target time array (fraction hour).
            height_new (ndarray): 1-D target height array (m).

        """
        for key in self.data:
            self.data[key].rebin_data(self.time, time_new, self.height,
                                      height_new)

    def add_meta(self):
        """Copies misc. metadata from the input file."""
        self._append_data(self._getvar('wavelength'), 'lidar_wavelength')
        self._append_data(config.BETA_ERROR[0], 'beta_bias')
        self._append_data(config.BETA_ERROR[1], 'beta_error')


class Mwr(RawDataSource):
    """Microwave radiometer class, child of RawDataSource.

    Args:
         mwr_file (str): File name of the calibrated mwr file.

    """
    def __init__(self, mwr_file):
        super().__init__(mwr_file)
        self._get_lwp_data()
        self._fix_time()

    def _get_lwp_data(self):
        key = utils.findkey(self.variables, ('LWP_data', 'lwp'))
        self._append_data(self.variables[key], 'lwp')
        self._append_data(self._calc_lwp_error(), 'lwp_error')

    def _calc_lwp_error(self):
        fractional_error, linear_error = config.LWP_ERROR
        return utils.l2norm(self.data['lwp'][:]*fractional_error, linear_error)

    def interpolate_to_cloudnet_grid(self, time):
        """Interpolates liquid water path to Cloudnet's dense time grid."""
        for key in self.data:
            fun = interp1d(self.time, self.data[key][:])
            self._append_data(fun(time), key)


class Model(RawDataSource):
    """Model class, child of RawDataSource.

    Args:
        model_file (str): File name of the NWP model file.
        alt_site (float): Altitude of the site above mean sea level (m).

    Attributes:
        type (str): Model type, e.g. 'gdas1' or 'ecwmf'.
        model_heights (ndarray): 2-D array of model heights (one for each time
            step).
        mean_height (ndarray): Mean of *model_heights*.
        data_sparse (dict): Model variables in common height grid but without
            interpolation in time.
        data_dense (dict): Model variables interpolated to Cloudnet's dense
            time / height grid.

    """
    fields_dense = ('temperature', 'pressure', 'rh',
                    'gas_atten', 'specific_gas_atten',
                    'specific_saturated_gas_atten',
                    'specific_liquid_atten')
    fields_sparse = fields_dense + ('q', 'uwind', 'vwind')

    def __init__(self, model_file, alt_site):
        super().__init__(model_file)
        self.type = self._get_model_type()
        self.model_heights = self._get_model_heights(alt_site)
        self.mean_height = self._get_mean_height()
        self.netcdf_to_cloudnet(self.fields_sparse)
        self.data_sparse = {}
        self.data_dense = {}

    def _get_model_type(self):
        possible_keys = ('ecmwf', 'gdas')
        for key in possible_keys:
            if key in self.filename:
                return key
        return ''

    def _get_model_heights(self, alt_site):
        """Returns model heights for each time step."""
        return utils.km2m(self.variables['height']) + alt_site

    def _get_mean_height(self):
        return np.mean(np.array(self.model_heights), axis=0)

    def interpolate_to_common_height(self, wl_band):
        """Interpolates model variables to common height grid.

        Args:
            wl_band (int): Integer denoting the wavelength band of the
                radar (0=35.5 GHz, 1=94 GHz).

        """
        def _interpolate_variable():
            datai = np.zeros((len(self.time), len(self.mean_height)))
            for ind, (alt, prof) in enumerate(zip(self.model_heights, data)):
                fun = interp1d(alt, prof, fill_value='extrapolate')
                datai[ind, :] = fun(self.mean_height)
            return CloudnetArray(datai, key, units)

        for key in self.fields_sparse:
            variable = self.variables[key]
            data = np.array(variable[:])
            units = variable.units
            if 'atten' in key:
                data = data[wl_band, :, :]
            self.data_sparse[key] = _interpolate_variable()
        self._append_data(self.time, 'model_time')
        self._append_data(self.mean_height, 'model_height')

    def interpolate_to_cloudnet_grid(self, time_new, height_new):
        """Interpolates model variables to Cloudnet's dense time / height grid.

        Args:
            time_new (ndarray): The target time array (fraction hour).
            height_new (ndarray): The target height array (m).

        """
        for key in self.fields_dense:
            self.data_dense[key] = utils.interpolate_2d(self.time,
                                                        self.mean_height,
                                                        self.data_sparse[key][:],
                                                        time_new, height_new)

    def calc_wet_bulb(self):
        """Calculates wet-bulb temperature in dense grid."""
        wet_bulb_temp = atmos.wet_bulb(self.data_dense['temperature'],
                                       self.data_dense['pressure'],
                                       self.data_dense['rh'])
        self._append_data(wet_bulb_temp, 'Tw', units='K')

    def screen_fields(self):
        """Removes model fields that we don't want to write in the output."""
        fields_to_keep = ('temperature', 'pressure', 'q', 'uwind', 'vwind')
        self.data_sparse = {key: self.data_sparse[key]
                            for key in fields_to_keep}


def generate_categorize(input_files, output_file, zlib=True):
    """ High-level API to generate Cloudnet categorize file.

    The measurements are rebinned into a common height / time grid,
    and classified as different types of scatterers such as ice, liquid,
    insects, etc. Next, the radar signal is corrected for atmospheric
    attenuation, and error estimates are computed. Results are saved
    in *ouput_file* which is by default a compressed NETCDF4_CLASSIC
    file.

    Args:
        input_files (tuple): Tuple of strings containing full paths of
                             the 4 input files (radar, lidar, mwr, model).
        output_file (str): Full path of the output file.
        zlib (bool): If True, the output file is compressed. Default is True.

    Examples:
        >>> from cloudnetpy import generate_categorize
        >>> generate_categorize(('radar.nc', 'lidar.nc', 'mwr.nc', 'model.nc'), 'output.nc')

    """
    def _interpolate_to_cloudnet_grid():
        """ Interpolate variables to Cloudnet's dense grid."""
        model.interpolate_to_common_height(radar.wl_band)
        model.interpolate_to_cloudnet_grid(*grid)
        model.calc_wet_bulb()
        mwr.interpolate_to_cloudnet_grid(final_time)
        radar.rebin_data(final_time)
        lidar.rebin_data(*grid)

    radar = Radar(input_files[0], ('Zh', 'v', 'ldr', 'width'))
    lidar = Lidar(input_files[1], ('beta',))
    mwr = Mwr(input_files[2])
    model = Model(input_files[3], radar.altitude)
    final_time, final_height = utils.time_grid(), radar.height
    grid = (final_time, final_height)

    _interpolate_to_cloudnet_grid()

    cbits, cbits_aux = classify.classify_measurements(radar.data['Zh'][:],
                                                      radar.data['v'][:],
                                                      radar.data['width'][:],
                                                      radar.data['ldr'][:],
                                                      lidar.data['beta'][:],
                                                      model.data['Tw'][:],
                                                      grid, model.type)

    gas_atten = atmos.gas_atten(model.data_dense,
                                cbits['category_bits'][:],
                                final_height)

    liq_atten, liq_atten_aux = atmos.liquid_atten(mwr.data,
                                                  model.data_dense,
                                                  cbits['category_bits'][:],
                                                  cbits_aux['liquid_bases'],
                                                  cbits_aux['is_rain'],
                                                  final_height)

    radar.correct_atten(gas_atten['radar_gas_atten'][:],
                        liq_atten['radar_liquid_atten'][:])

    quality_bits = classify.fetch_qual_bits(radar.data['Z'][:],
                                            lidar.data['beta'][:],
                                            cbits_aux['is_clutter'],
                                            liq_atten_aux)

    radar.calc_errors(gas_atten['radar_gas_atten'][:],
                      liq_atten_aux['liq_att_err'],
                      liq_atten_aux['is_not_corr'],
                      cbits_aux['is_clutter'])
    radar.add_meta()
    lidar.add_meta()
    model.screen_fields()

    output_data = {**radar.data, **lidar.data, **model.data_sparse,
                   **mwr.data, **cbits, **gas_atten, **liq_atten,
                   **quality_bits}

    output.update_attributes(output_data)
    _save_cat(output_file, radar, lidar, model, output_data, zlib)


def _save_cat(file_name, radar, lidar, model, obs, zlib):
    """Creates a categorize netCDF4 file and saves all data into it."""
    dims = {
        'time': len(radar.time),
        'height': len(radar.height),
        'model_time': len(model.time),
        'model_height': len(model.mean_height)}
    rootgrp = output.init_file(file_name, dims, obs, zlib)
    output.copy_global(radar.dataset, rootgrp, ('year', 'month', 'day'))
    rootgrp.title = f"Categorize file from {radar.location}"
    rootgrp.references = 'https://doi.org/10.1175/BAMS-88-6-883'
    _merge_history(rootgrp, radar)
    _merge_source(rootgrp, radar, lidar)
    rootgrp.close()


def _merge_history(rootgrp, radar):
    radar_history = radar.dataset.history
    cat_history = f"{utils.get_time()} - categorize file created"
    rootgrp.history = f"{cat_history}\n{radar_history}"


def _merge_source(rootgrp, radar, lidar):
    rootgrp.source = f"radar: {radar.source}\nlidar: {lidar.source}"
