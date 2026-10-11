#ifndef FASTLIO_VOXEL_CENTROIDS_H
#define FASTLIO_VOXEL_CENTROIDS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
#define FASTLIO_VOXEL_NOEXCEPT noexcept
extern "C" {
#else
#define FASTLIO_VOXEL_NOEXCEPT
#endif

enum fastlio_voxel_status {
    FASTLIO_VOXEL_OK = 0,
    FASTLIO_VOXEL_INVALID_ARGUMENT = 1,
    FASTLIO_VOXEL_RANGE_ERROR = 2,
    FASTLIO_VOXEL_ALLOCATION_ERROR = 3,
    FASTLIO_VOXEL_INTERNAL_ERROR = 4
};

struct fastlio_voxel_result {
    float* centroids_xyzi;
    int64_t* counts;
    size_t voxel_count;
};

/*
 * Average finite row-major double XYZI samples. XYZ and voxel_size_m are metres.
 * Keys are floor(XYZ / voxel_size_m), relative to zero, with abs(key) < 2^62.
 * Results use float32 XYZI and int64 counts, sorted by integer X, then Y, then Z.
 * Double sum overflow or a centroid outside finite float32 range is an error.
 *
 * Initialize result to zero; release it before reusing it for another call.
 * On failure result is empty. On success, release it using this library's
 * fastlio_voxel_free_result. No C++ exception crosses this C ABI.
 * points_xyzi may be NULL only when point_count is zero. The caller is
 * responsible for a readable buffer containing point_count * 4 doubles.
 * error_message is optional; a non-NULL buffer with positive error_capacity
 * receives a NUL-terminated diagnostic (possibly truncated), or an empty string
 * on success. Buffers must not overlap the input or result structure.
 */
int fastlio_voxel_centroids(const double* points_xyzi, size_t point_count,
                           double voxel_size_m,
                           struct fastlio_voxel_result* result,
                           char* error_message, size_t error_capacity)
    FASTLIO_VOXEL_NOEXCEPT;

/* NULL and an already freed, zeroed result are safe. Never free these buffers
 * with the caller's allocator or modify the stored pointers before release. */
void fastlio_voxel_free_result(struct fastlio_voxel_result* result)
    FASTLIO_VOXEL_NOEXCEPT;

#ifdef __cplusplus
}
#endif

#undef FASTLIO_VOXEL_NOEXCEPT
#endif
