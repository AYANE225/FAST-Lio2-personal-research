"""Call the explicitly built C++17 voxel library through its C ABI.

Build once with ``cmake -S . -B output/native-build -DCMAKE_BUILD_TYPE=Release``
and ``cmake --build output/native-build``. Calls never compile the library or
fall back to another backend. The library owns native result memory until it is
copied into independent NumPy arrays and released with the same library.
"""
import ctypes
from functools import lru_cache
import numbers
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIBRARY = ROOT / 'output/native-build/libfastlio_voxel.so'


class _VoxelResult(ctypes.Structure):
    _fields_ = [('centroids_xyzi', ctypes.POINTER(ctypes.c_float)),
               ('counts', ctypes.POINTER(ctypes.c_int64)),
               ('voxel_count', ctypes.c_size_t)]


@lru_cache(maxsize=8)
def _load_library(path):
    library = ctypes.CDLL(path)
    try:
        library.fastlio_voxel_centroids.argtypes = [
            ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_double,
            ctypes.POINTER(_VoxelResult), ctypes.POINTER(ctypes.c_char), ctypes.c_size_t]
        library.fastlio_voxel_centroids.restype = ctypes.c_int
        library.fastlio_voxel_free_result.argtypes = [ctypes.POINTER(_VoxelResult)]
        library.fastlio_voxel_free_result.restype = None
    except AttributeError as error:
        raise OSError(f'Native voxel shared library has an incompatible C ABI: {path}') from error
    return library


def voxel_centroids_native(points, voxel_size_m, library_path=None):
    """Return float32 XYZI centroids and int64 counts in integer XYZ key order.

    ``points`` must be a real numeric N x 4 array of finite XYZI values. XYZ and
    ``voxel_size_m`` are metres; bins use ``floor(XYZ / voxel_size_m)`` with zero
    origin and ``abs(key) < 2**62``. Inputs are converted directly to contiguous,
    aligned float64, preserving existing float64 precision. Accumulation uses double;
    nonfinite sums or centroids outside finite float32 range raise ValueError.

    The default library path is relative to this repository, independent of the
    current directory. A supplied path is resolved relative to the caller's
    directory. Missing libraries raise FileNotFoundError, even for empty input.
    """
    points = np.asarray(points)
    if points.ndim != 2 or points.shape[1] != 4:
        raise ValueError('Expected N x 4 XYZ/intensity points')
    if points.dtype.kind not in 'fiu':
        raise ValueError('XYZ/intensity dtype must be real numeric, excluding bool')
    if (isinstance(voxel_size_m, (bool, np.bool_))
            or not isinstance(voxel_size_m, numbers.Real)):
        raise ValueError('voxel_size_m must be a finite positive real scalar')
    try:
        voxel_size_m = float(voxel_size_m)
    except (OverflowError, ValueError) as error:
        raise ValueError('voxel_size_m must be finite and positive') from error
    if not np.isfinite(voxel_size_m) or voxel_size_m <= 0:
        raise ValueError('voxel_size_m must be finite and positive')
    with np.errstate(over='ignore', invalid='ignore'):
        points = np.require(points, dtype=np.float64, requirements=['C', 'A'])
    if not np.isfinite(points).all():
        raise ValueError('XYZ/intensity samples must be finite as float64')

    path = DEFAULT_LIBRARY if library_path is None else Path(library_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f'Native voxel shared library not found: {path}. '
            'Build it with cmake --build output/native-build after CMake configuration.')
    library = _load_library(str(path))
    result = _VoxelResult()
    error = ctypes.create_string_buffer(512)
    try:
        status = library.fastlio_voxel_centroids(
            points.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), len(points),
            voxel_size_m, ctypes.byref(result), error, len(error))
        if status != 0:
            message = error.value.decode('utf-8', errors='replace') or f'Native voxel error {status}'
            if status in (1, 2):
                raise ValueError(message)
            if status == 3:
                raise MemoryError(message)
            raise RuntimeError(message)
        if result.voxel_count == 0:
            return np.empty((0, 4), dtype=np.float32), np.empty(0, dtype=np.int64)
        centroids = np.ctypeslib.as_array(
            result.centroids_xyzi, shape=(result.voxel_count * 4,)).reshape(-1, 4).copy()
        counts = np.ctypeslib.as_array(result.counts, shape=(result.voxel_count,)).copy()
        return centroids, counts
    finally:
        library.fastlio_voxel_free_result(ctypes.byref(result))
