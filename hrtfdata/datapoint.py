from .query import DataQuery, CipicDataQuery, AriDataQuery, ListenDataQuery, BiLiDataQuery, ItaDataQuery, HutubsDataQuery, RiecDataQuery, ChedarDataQuery, WidespreadDataQuery, Sadie2DataQuery, ThreeDThreeADataQuery, SonicomDataQuery
from .util import wrap_closed_open_interval, spherical2cartesian, spherical2interaural, cartesian2spherical, cartesian2interaural, interaural2spherical, interaural2cartesian
from abc import abstractmethod
from pathlib import Path
import numpy as np
import netCDF4 as ncdf
from scipy.fft import rfft, fftfreq
from PIL import Image


class DataPoint:

    def __init__(
        self,
        query: DataQuery,
        dataset_id: str,
        verbose: bool = False,
        dtype: type = np.float32,
    ):
        self.query = query
        self.dataset_id = dataset_id
        self.verbose = verbose
        self.dtype = dtype


class SofaDataPoint(DataPoint):
    """
    An abstract class that reads the HRIR for a given subject id of a dataset from a SOFA file and stores it internally
    as a 3D tensor. The HRIR directions are stored in the rows and columns as a plate carrée projection where each
    column represents a plane parallel to the fundamental plane, i.e. each row represents a single angle in the
    fundamental plane. The poles (if present) are therefore stored in the first and last column.

    row_angle: angle in the fundamental plane with range  [-180, 180)
    (azimuth for spherical coordinates, vertical angle for interaural coordinates)
    column_angle: angle between fundamental plane and directional vector with range [-90, 90]
    (elevation for spherical coordinates, lateral angle for interaurl coordinates)
    """

    _quantisation: int = 3

    @abstractmethod
    def _sofa_path(self, subject_id):
        pass
    

    def hrir_samplerate(self, subject_id):
        sofa_path = self._sofa_path(subject_id)
        hrir_file = ncdf.Dataset(sofa_path)
        try:
            samplerate = hrir_file.variables['Data.SamplingRate'][:].item()
        except:
            raise ValueError(f'Error reading file "{sofa_path}"')
        finally:
            hrir_file.close()
        return samplerate


    def hrir_length(self, subject_id):
        sofa_path = self._sofa_path(subject_id)
        hrir_file = ncdf.Dataset(sofa_path)
        try:
            length = hrir_file.dimensions['N'].size
        except:
            raise ValueError(f'Error reading file "{sofa_path}"')
        finally:
            hrir_file.close()
        return length


    def hrtf_frequencies(self, subject_id):
        num_samples = self.hrir_length(subject_id)
        num_bins = num_samples // 2 + 1
        return np.abs(fftfreq(num_samples, 1./self.hrir_samplerate(subject_id))[:num_bins])


    @staticmethod
    def _hrir_select_angles(row_angles, column_angles, all_row_angles, all_column_angles, position_mask):
        if row_angles is None:
            select_row_indices = np.full(len(all_row_angles), True)
        else:
            select_row_indices = np.array([np.isclose(angle, row_angles).any() for angle in all_row_angles])
            if not any(select_row_indices):
                raise ValueError('None of the specified angles are available in this dataset')

        if column_angles is None:
            select_column_indices = np.full(len(all_column_angles), True)
        else:
            select_column_indices = np.array([np.isclose(angle, column_angles).any() for angle in all_column_angles])
            if not any(select_column_indices):
                raise ValueError('None of the specified angles are available in this dataset')

        selected_position_mask = position_mask[select_row_indices][:, select_column_indices]
        # prune those row indices that no longer have a single colum index in the current selection and the other way around
        keep_row_indices = ~selected_position_mask.all(axis=(1,2))
        keep_column_indices = ~selected_position_mask.all(axis=(0,2))
        row_indices = select_row_indices.nonzero()[0][keep_row_indices]
        column_indices = select_column_indices.nonzero()[0][keep_column_indices]

        return row_indices, column_indices


    def _map_sofa_position_order_to_matrix(self, subject_id):
        sofa_path = self._sofa_path(subject_id)
        hrir_file = ncdf.Dataset(sofa_path)
        try:
            positions = np.ma.getdata(hrir_file.variables['SourcePosition'][:])
            if isinstance(self, SofaInterauralDataPoint):
                if hrir_file.variables['SourcePosition'].Type == 'cartesian':
                    positions = np.stack(cartesian2interaural(*positions.T), axis=1)
                else:
                    positions = np.stack(spherical2interaural(*positions.T), axis=1)
                positions[:, [0, 1]] = positions[:, [1, 0]]
            elif hrir_file.variables['SourcePosition'].Type == 'cartesian':
                positions = np.stack(cartesian2spherical(*positions.T), axis=1)
        except:
            raise ValueError(f'Error reading file "{sofa_path}"')
        finally:
            hrir_file.close()
        quantised_positions = np.round(positions, self._quantisation)
        quantised_positions[:, 0] = wrap_closed_open_interval(quantised_positions[:, 0], -180, 180)
        unique_row_angles = np.unique(quantised_positions[:, 0])
        unique_column_angles = np.unique(quantised_positions[:, 1])
        unique_radii = np.unique(quantised_positions[:, 2])
        position_map = np.empty((3, len(positions)), dtype=int)
        for idx, (row_angle, column_angle, radius) in enumerate(quantised_positions):
            position_map[:, idx] = np.argmax(row_angle == unique_row_angles), np.argmax(column_angle == unique_column_angles), np.argmax(radius == unique_radii)
        position_map = tuple(position_map)
        
        position_mask = np.full((len(unique_row_angles), len(unique_column_angles), len(unique_radii)), True)
        position_mask[position_map] = False

        # If pole values (column_angle 90 and/or -90) present for single row_angle, copy to all row_angles
        if np.isclose(unique_column_angles[-1], 90):
            single_end_mask = np.sum(~position_mask[:, -1], axis=0) == 1 # boolean for each radius value
            end_pole_idx = (~position_mask[:, -1]).argmax()
            position_mask[:, -1] = np.where(single_end_mask, False, position_mask[:, -1])
        else:
            single_end_mask = False
            end_pole_idx = 0
        if np.isclose(unique_column_angles[0], -90):
            single_start_mask = np.sum(~position_mask[:, 0], axis=0) == 1
            start_pole_idx = (~position_mask[:, 0]).argmax()
            position_mask[:, 0] = np.where(single_start_mask, False, position_mask[:, 0])
        else:
            single_start_mask = False
            start_pole_idx = 0
        
        return unique_row_angles, unique_column_angles, unique_radii, position_mask, position_map, single_start_mask, start_pole_idx, single_end_mask, end_pole_idx


    # called by torch.full
    def hrir_angle_indices(self, subject_id, row_angles=None, column_angles=None):
        unique_row_angles, unique_column_angles, unique_radii, position_mask, *_ = self._map_sofa_position_order_to_matrix(subject_id)
        row_indices, column_indices = SofaDataPoint._hrir_select_angles(row_angles, column_angles, unique_row_angles, unique_column_angles, position_mask)
        selected_angles = {unique_row_angles[row_idx]: np.ma.array(unique_column_angles[column_indices], mask=position_mask[row_idx, column_indices]) for row_idx in row_indices}
        return selected_angles, row_indices, column_indices


    def hrir_positions(self, subject_id, coordinate_system, row_angles=None, column_angles=None):
        unique_row_angles, unique_column_angles, unique_radii, position_mask, *_ = self._map_sofa_position_order_to_matrix(subject_id)
        row_indices, column_indices = SofaDataPoint._hrir_select_angles(row_angles, column_angles, unique_row_angles, unique_column_angles, position_mask)
        selected_position_mask = position_mask[row_indices][:, column_indices]
        selected_row_angles = unique_row_angles[row_indices]
        selected_column_angles = unique_column_angles[column_indices]

        if isinstance(self, SofaSphericalDataPoint):
            if coordinate_system == 'spherical':
                coordinates = selected_row_angles, selected_column_angles, unique_radii
            elif coordinate_system == 'interaural':
                coordinates = spherical2interaural(selected_row_angles, selected_column_angles, unique_radii)
            elif coordinate_system == 'cartesian':
                coordinates = spherical2cartesian(selected_row_angles, selected_column_angles, unique_radii)
            else:
                raise ValueError(f'Unknown coordinate system "{coordinate_system}"')
        else:
            if coordinate_system == 'interaural':
                coordinates = selected_row_angles, selected_column_angles, unique_radii
            elif coordinate_system == 'spherical':
                coordinates = interaural2spherical(selected_column_angles, selected_row_angles, unique_radii)
                coordinates[0], coordinates[1] = coordinates[1], coordinates[0]
            elif coordinate_system == 'cartesian':
                coordinates = interaural2cartesian(selected_row_angles, selected_row_angles, unique_radii)
                coordinates[0], coordinates[1] = coordinates[1], coordinates[0]
            else:
                raise ValueError(f'Unknown coordinate system "{coordinate_system}"')

        position_grid = np.stack(np.meshgrid(*coordinates, indexing='ij'), axis=-1)
        if selected_position_mask.any(): # sparse grid
            tiled_position_mask = np.tile(selected_position_mask[:, :, :, np.newaxis], (1, 1, 1, 3))
            return np.ma.masked_where(tiled_position_mask, position_grid)
        # dense grid
        return position_grid


    def hrir(self, subject_id, side, domain='time', row_indices=None, column_indices=None):
        sofa_path = self._sofa_path(subject_id)
        hrir_file = ncdf.Dataset(sofa_path)
        try:
            hrirs = np.ma.getdata(hrir_file.variables['Data.IR'][:, 0 if side.endswith('left') else 1, :])
        except:
            raise ValueError(f'Error reading file "{sofa_path}"')
        finally:
            hrir_file.close()
        unique_row_angles, _, _, position_mask, position_map, single_start_mask, start_pole_idx, single_end_mask, end_pole_idx = self._map_sofa_position_order_to_matrix(subject_id)
        hrir_matrix = np.empty(position_mask.shape + (hrirs.shape[1],))
        hrir_matrix[position_map] = hrirs
        hrir_matrix[:, 0] = np.where(single_start_mask, hrir_matrix[start_pole_idx, 0], hrir_matrix[:, 0])
        hrir_matrix[:, -1] = np.where(single_end_mask, hrir_matrix[end_pole_idx, -1], hrir_matrix[:, -1])

        if row_indices is None:
            row_indices = slice(None)
        if column_indices is None:
            column_indices = slice(None)

        selected_position_mask = position_mask[row_indices][:, column_indices]
        tiled_position_mask = np.tile(selected_position_mask[:, :, :, np.newaxis], (1, 1, 1, hrirs.shape[1]))
        selected_hrir_matrix = hrir_matrix[row_indices][:, column_indices]
        if domain == 'time':
            hrir = np.ma.masked_where(tiled_position_mask, selected_hrir_matrix, copy=False)
        else:
            selected_hrtf_matrix = np.ma.masked_where(tiled_position_mask[:, :, :, :hrirs.shape[1]//2+1], rfft(selected_hrir_matrix), copy=False)
            if domain == 'complex':
                hrir = selected_hrtf_matrix
            elif domain.startswith('magnitude'):
                magnitudes = np.abs(selected_hrtf_matrix)
                if domain.endswith('_db'):
                    # limit dB range to what is representable in data type
                    min_magnitude = np.max(magnitudes) * np.finfo(self.dtype).resolution
                    hrir = 20*np.log10(np.clip(magnitudes, min_magnitude, None))
                else:
                    hrir = magnitudes
            elif domain == 'phase':
                hrir = np.angle(selected_hrtf_matrix)
            else:
                hrir = ValueError(f'Unknown domain "{domain}" for HRIR')
        if domain == 'complex' and not issubclass(self.dtype, np.complexfloating):
            raise ValueError(f'An HRTF in the complex domain requires the dtype to be set to a complex type (currently {self.dtype})')
        hrir = np.squeeze(hrir.astype(self.dtype))
        if side.startswith('mirrored'):
            if isinstance(self, SofaSphericalDataPoint):
                # flip azimuths (in rows)
                selected_azimuths = unique_row_angles[row_indices]
                if np.isclose(selected_azimuths[0], -180):
                    return np.ma.row_stack((hrir[0:1], np.flipud(hrir[1:])))
                else:
                    return np.flipud(hrir)
            else:
                # flip lateral angles (in columns)
                return np.fliplr(hrir)
        return hrir


class SofaSphericalDataPoint(SofaDataPoint):

    def hrir_positions(self, subject_id, row_angles=None, column_angles=None, coordinate_system='spherical'):
        return super().hrir_positions(subject_id, coordinate_system, row_angles, column_angles)


class SofaInterauralDataPoint(SofaDataPoint):

    def hrir_positions(self, subject_id, row_angles=None, column_angles=None, coordinate_system='interaural'):
        return super().hrir_positions(subject_id, coordinate_system, row_angles, column_angles)


class MatFileAnthropometryDataPoint(DataPoint):

    def anthropomorphic_data(self, subject_id, side=None, select=None):
        select_all = ('head-torso', 'pinna-size', 'pinna-angle', 'weight', 'age', 'sex')
        if select is None:
            select = select_all
        elif isinstance(select, str):
            select = (select,)
        # if 'pinna-size' not in select and 'pinna-angle' not in select:
        #     if side is not None:
        #         print(f'Side "{side}" is irrelevant for this measurements selection "{", ".join(select)}"')
        # el
        if side not in ['left', 'right', 'both']: # and ('pinna-size' in select or 'pinna-angle' in select)
            raise ValueError(f'Unknown side selector "{side}"')

        unknown_select = sorted(set(select) - set(select_all))
        if unknown_select:
            raise ValueError(f'Unknown selection "{unknown_select}". Choose one or more from "{select_all}"')

        subject_idx = np.squeeze(np.argwhere(np.squeeze(self.anth['id']) == subject_id))
        if subject_idx.size == 0:
            raise ValueError(f'Subject id "{subject_id}" has no anthropomorphic measurements')

        select_data = []

        if 'head-torso' in select:
            select_data.append(self.anth['X'][subject_idx])
        if side == 'left' or side.startswith('both'):
            if 'pinna-size' in select:
                select_data.append(self.anth['D'][subject_idx, :8])
            if 'pinna-angle' in select:
                select_data.append(self.anth['theta'][subject_idx, :2])
        if side == 'right' or side.startswith('both'):
            if 'pinna-size' in select:
                select_data.append(self.anth['D'][subject_idx, 8:])
            if 'pinna-angle' in select:
                select_data.append(self.anth['theta'][subject_idx, 2:])
        if 'weight' in select:
            select_data.append(self.anth['WeightKilograms'][subject_idx])
        if 'age' in select:
            select_data.append(self.anth['age'][subject_idx])
        if 'sex' in select:
            select_data.append(0 if self.anth['sex'][subject_idx] == 'M' else 1 if self.anth['sex'][subject_idx] == 'F' else np.nan)

        selected_data = np.hstack(select_data).astype(self.dtype)
        if np.all(np.isnan(selected_data), axis=-1):
            raise ValueError(f'Subject id "{subject_id}" has no data available for selection "{", ".join(select)}"')
        return selected_data


class ImageDataPoint(DataPoint):

    @abstractmethod
    def _image_path(self, subject_id, side=None, rear=False):
        pass


    def image(self, subject_id, side=None, rear=False):
        img = Image.open(self.pinna_image_path(subject_id, side, rear))
        if side.startwith('mirrored-'):
            return img.transpose(Image.FLIP_LEFT_RIGHT)
        return img


class CipicDataPoint(SofaInterauralDataPoint, ImageDataPoint, MatFileAnthropometryDataPoint):

    def __init__(
        self,
        sofa_directory_path=None,
        image_directory_path=None,
        anthropomorphy_matfile_path=None,
        verbose=False,
        dtype=np.float32
    ):
        query = CipicDataQuery(sofa_directory_path, image_directory_path, anthropomorphy_matfile_path)
        super().__init__(query, 'cipic', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / 'subject_{:03d}.sofa'.format(subject_id))


class AriDataPoint(SofaSphericalDataPoint, MatFileAnthropometryDataPoint):

    def __init__(
        self,
        sofa_directory_path=None,
        anthropomorphy_matfile_path=None,
        verbose=False,
        dtype=np.float32
    ):
        query = AriDataQuery(sofa_directory_path, anthropomorphy_matfile_path)
        super().__init__(query, 'ari', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(next(self.query.sofa_directory_path.glob('hrtf [bc]_nh{}.sofa'.format(subject_id))))


class ListenDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, verbose=False, dtype=np.float32):
        query = ListenDataQuery(sofa_directory_path)
        super().__init__(query, 'listen', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / 'IRC_{:04d}_C_44100.sofa'.format(subject_id))


class BiLiDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, verbose=False, dtype=np.float32):
        query = BiLiDataQuery(sofa_directory_path)
        super().__init__(query, 'bili', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / 'IRC_{:04d}_C_HRIR_96000.sofa'.format(subject_id))


class ItaDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, verbose=False, dtype=np.float32):
        query = ItaDataQuery(sofa_directory_path)
        super().__init__(query, 'ita', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / 'MRT{:02d}.sofa'.format(subject_id))


class HutubsDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, verbose=False, dtype=np.float32):
        query = HutubsDataQuery(sofa_directory_path)
        super().__init__(query, 'hutubs', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / 'pp{:d}_HRIRs_measured.sofa'.format(subject_id))


class RiecDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, verbose=False, dtype=np.float32):
        query = RiecDataQuery(sofa_directory_path)
        super().__init__(query, 'riec', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / f'RIEC_hrir_subject_{subject_id:03d}.sofa')


class ChedarDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, radius=1, verbose=False, dtype=np.float32):
        query = ChedarDataQuery(sofa_directory_path)
        super().__init__(query, 'chedar', verbose, dtype)
        if np.isclose(radius, 0.2):
            self.radius = '02m'
        elif np.isclose(radius, 0.5):
            self.radius = '05m'
        elif np.isclose(radius, 1):
            self.radius = '1m'
        elif np.isclose(radius, 2):
            self.radius = '2m'
        else:
            raise ValueError('The radius needs to be one of 0.2, 0.5, 1 or 2')
        self._quantisation = 0


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / f'chedar_{subject_id:04d}_UV{self.radius}.sofa')


class WidespreadDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, radius=1, verbose=False, dtype=np.float32):
        query = WidespreadDataQuery(sofa_directory_path)
        super().__init__(query, 'widespread', verbose, dtype)
        if np.isclose(radius, 0.2):
            self.radius = '02m'
        elif np.isclose(radius, 0.5):
            self.radius = '05m'
        elif np.isclose(radius, 1):
            self.radius = '1m'
        elif np.isclose(radius, 2):
            self.radius = '2m'
        else:
            raise ValueError('The radius needs to be one of 0.2, 0.5, 1 or 2')
        self._quantisation = 0


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / f'UV{self.radius}_{subject_id:05d}.sofa')


class Sadie2DataPoint(SofaSphericalDataPoint, ImageDataPoint):

    def __init__(self, sofa_directory_path=None, image_directory_path=None, verbose=False, dtype=np.float32):
        query = Sadie2DataQuery(sofa_directory_path, image_directory_path)
        super().__init__(query, 'sadie2', verbose, dtype)


    def _sofa_path(self, subject_id):
        if subject_id < 3:
            sadie2_id = f'D{subject_id}'
        else:
            sadie2_id = f'H{subject_id}'
        return str(self.query.sofa_directory_path / f'{sadie2_id}/{sadie2_id}_HRIR_SOFA/{sadie2_id}_96K_24bit_512tap_FIR_SOFA.sofa')


class ThreeDThreeADataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, verbose=False, dtype=np.float32):
        query = ThreeDThreeADataQuery(sofa_directory_path)
        super().__init__(query, '3d3a', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / f'Subject{subject_id}_HRIRs.sofa')


class SonicomDataPoint(SofaSphericalDataPoint):

    def __init__(self, sofa_directory_path, verbose=False, dtype=np.float32):
        query = SonicomDataQuery(sofa_directory_path)
        super().__init__(query, 'sonicom', verbose, dtype)


    def _sofa_path(self, subject_id):
        return str(self.query.sofa_directory_path / f'P{subject_id:04d}/HRTF/{self.query._samplerate_str}/P{subject_id:04d}_{self.query._hrtf_variant_str}_{self.query._samplerate_str}.sofa')
