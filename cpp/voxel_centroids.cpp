#include "voxel_centroids.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>
#include <unordered_map>
#include <vector>

namespace {

using VoxelKey = std::array<int64_t, 3>;

struct VoxelKeyHash {
    size_t operator()(const VoxelKey& key) const noexcept {
        size_t hash = 0;
        for (const int64_t coordinate : key) {
            hash ^= std::hash<int64_t>{}(coordinate)
                    + static_cast<size_t>(0x9e3779b97f4a7c15ULL)
                    + (hash << 6U) + (hash >> 2U);
        }
        return hash;
    }
};

struct VoxelAccumulator {
    std::array<double, 4> sums{};
    int64_t count = 0;
};

void set_error(char* destination, size_t capacity, const char* message) noexcept {
    if (destination != nullptr && capacity > 0) {
        const size_t length = std::min(std::strlen(message), capacity - 1);
        std::memcpy(destination, message, length);
        destination[length] = '\0';
    }
}

}  // namespace

int fastlio_voxel_centroids(const double* points_xyzi, size_t point_count,
                           double voxel_size_m, fastlio_voxel_result* result,
                           char* error_message, size_t error_capacity) noexcept {
    set_error(error_message, error_capacity, "");
    if (result == nullptr) {
        set_error(error_message, error_capacity, "result must not be NULL");
        return FASTLIO_VOXEL_INVALID_ARGUMENT;
    }
    *result = {};

    try {
        if (!std::isfinite(voxel_size_m) || voxel_size_m <= 0) {
            throw std::invalid_argument("voxel_size_m must be finite and positive");
        }
        if (points_xyzi == nullptr && point_count != 0) {
            throw std::invalid_argument("points_xyzi must not be NULL for nonempty input");
        }
        // Bound both pointer arithmetic and the signed occupancy counters before
        // reading any caller memory or allocating storage.
        if (point_count > static_cast<size_t>(std::numeric_limits<ptrdiff_t>::max())
                              / (4 * sizeof(double))
            || point_count > static_cast<size_t>(std::numeric_limits<int64_t>::max())) {
            throw std::overflow_error("point_count exceeds supported array range");
        }
        if (point_count == 0) {
            return FASTLIO_VOXEL_OK;
        }

        using VoxelTable = std::unordered_map<VoxelKey, VoxelAccumulator, VoxelKeyHash>;
        VoxelTable voxels;
        voxels.reserve(point_count);
        constexpr double key_limit = 0x1p62;
        for (size_t point_index = 0; point_index < point_count; ++point_index) {
            const double* point = points_xyzi + point_index * 4;
            for (size_t column = 0; column < 4; ++column) {
                if (!std::isfinite(point[column])) {
                    throw std::invalid_argument("XYZ/intensity samples must be finite");
                }
            }
            VoxelKey key{};
            for (size_t axis = 0; axis < 3; ++axis) {
                const double coordinate = std::floor(point[axis] / voxel_size_m);
                if (!std::isfinite(coordinate) || coordinate <= -key_limit
                    || coordinate >= key_limit) {
                    throw std::overflow_error("Voxel coordinates exceed supported integer range");
                }
                key[axis] = static_cast<int64_t>(coordinate);
            }
            auto& accumulator = voxels[key];
            if (accumulator.count == std::numeric_limits<int64_t>::max()) {
                throw std::overflow_error("Voxel occupancy count overflow");
            }
            for (size_t column = 0; column < 4; ++column) {
                const double sum = accumulator.sums[column] + point[column];
                if (!std::isfinite(sum)) {
                    throw std::overflow_error("Double XYZI accumulation overflow");
                }
                accumulator.sums[column] = sum;
            }
            ++accumulator.count;
        }

        // Sort references to completed hash entries without copying their sums.
        std::vector<const VoxelTable::value_type*> ordered;
        ordered.reserve(voxels.size());
        for (const auto& voxel : voxels) {
            ordered.push_back(&voxel);
        }
        std::sort(ordered.begin(), ordered.end(), [](const auto* left, const auto* right) {
            return left->first < right->first;
        });

        const size_t voxel_count = ordered.size();
        std::unique_ptr<float[]> centroids(new float[voxel_count * 4]);
        std::unique_ptr<int64_t[]> counts(new int64_t[voxel_count]);
        constexpr double float_max = std::numeric_limits<float>::max();
        for (size_t voxel_index = 0; voxel_index < voxel_count; ++voxel_index) {
            const auto& accumulator = ordered[voxel_index]->second;
            counts[voxel_index] = accumulator.count;
            for (size_t column = 0; column < 4; ++column) {
                const double centroid = accumulator.sums[column]
                                        / static_cast<double>(accumulator.count);
                if (!std::isfinite(centroid) || std::abs(centroid) > float_max) {
                    throw std::overflow_error("Voxel centroid exceeds finite float32 range");
                }
                centroids[voxel_index * 4 + column] = static_cast<float>(centroid);
            }
        }

        // Publish only the complete result. Local owners clean up both arrays
        // if allocation or any numerical check above throws.
        result->centroids_xyzi = centroids.release();
        result->counts = counts.release();
        result->voxel_count = voxel_count;
        return FASTLIO_VOXEL_OK;
    } catch (const std::invalid_argument& error) {
        set_error(error_message, error_capacity, error.what());
        return FASTLIO_VOXEL_INVALID_ARGUMENT;
    } catch (const std::overflow_error& error) {
        set_error(error_message, error_capacity, error.what());
        return FASTLIO_VOXEL_RANGE_ERROR;
    } catch (const std::length_error& error) {
        set_error(error_message, error_capacity, error.what());
        return FASTLIO_VOXEL_RANGE_ERROR;
    } catch (const std::bad_alloc&) {
        set_error(error_message, error_capacity, "Native voxel memory allocation failed");
        return FASTLIO_VOXEL_ALLOCATION_ERROR;
    } catch (const std::exception& error) {
        set_error(error_message, error_capacity, error.what());
        return FASTLIO_VOXEL_INTERNAL_ERROR;
    } catch (...) {
        set_error(error_message, error_capacity, "Unknown native voxel error");
        return FASTLIO_VOXEL_INTERNAL_ERROR;
    }
}

void fastlio_voxel_free_result(fastlio_voxel_result* result) noexcept {
    if (result != nullptr) {
        delete[] result->centroids_xyzi;
        delete[] result->counts;
        *result = {};
    }
}
