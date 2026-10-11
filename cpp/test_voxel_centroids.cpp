#include "voxel_centroids.h"

#include <array>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {

void require(bool condition, const char* message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

struct Result {
    fastlio_voxel_result value{};
    ~Result() { fastlio_voxel_free_result(&value); }
};

void require_empty(const fastlio_voxel_result& result) {
    require(result.centroids_xyzi == nullptr, "failed call exposed centroids");
    require(result.counts == nullptr, "failed call exposed counts");
    require(result.voxel_count == 0, "failed call exposed a voxel count");
}

void test_centroids_and_order() {
    const double points[] = {
        .1, .1, 1.1, 50, .1, 1.1, .1, 60, -.2, 0, 0, 10,
        -.8, 0, 0, 30, .1, -.1, 5, 20, .1, .1, -.1, 30,
        .1, .1, .1, 40, .9, .9, .9, 80};
    const float expected[] = {
        -.5F, 0, 0, 20, .1F, -.1F, 5, 20, .1F, .1F, -.1F, 30,
        .5F, .5F, .5F, 60, .1F, .1F, 1.1F, 50, .1F, 1.1F, .1F, 60};
    const int64_t expected_counts[] = {2, 1, 1, 2, 1, 1};
    Result result;
    char error[128] = "old error";
    require(fastlio_voxel_centroids(points, 8, 1, &result.value, error,
                                   sizeof(error)) == FASTLIO_VOXEL_OK,
            "valid cloud must succeed");
    require(error[0] == '\0', "success must clear the diagnostic");
    require(result.value.voxel_count == 6, "incorrect voxel count");
    for (size_t i = 0; i < 24; ++i) {
        require(std::abs(result.value.centroids_xyzi[i] - expected[i]) < 1e-6F,
                "incorrect XYZI average or lexicographic key order");
    }
    for (size_t i = 0; i < 6; ++i) {
        require(result.value.counts[i] == expected_counts[i], "incorrect occupancy");
    }
}

void test_double_boundaries() {
    const double points[] = {std::nextafter(1., 0.), 0, 0, 10, 1, 0, 0, 30,
                             std::nextafter(-1., -2.), 0, 0, 50, -1, 0, 0, 70};
    const float expected_intensity[] = {50, 70, 10, 30};
    Result result;
    require(fastlio_voxel_centroids(points, 4, 1, &result.value, nullptr, 0)
                == FASTLIO_VOXEL_OK, "double boundary input must succeed");
    require(result.value.voxel_count == 4, "double precision bins were merged");
    for (size_t i = 0; i < 4; ++i) {
        require(result.value.counts[i] == 1, "boundary occupancy must be one");
        require(result.value.centroids_xyzi[i * 4 + 3] == expected_intensity[i],
                "boundary sample entered the wrong voxel");
    }
}

void test_empty_and_null_arguments() {
    Result result;
    char error[128]{};
    require(fastlio_voxel_centroids(nullptr, 0, 1, &result.value, error, sizeof(error))
                == FASTLIO_VOXEL_OK, "empty cloud must succeed");
    require_empty(result.value);
    require(fastlio_voxel_centroids(nullptr, 1, 1, &result.value, error, sizeof(error))
                == FASTLIO_VOXEL_INVALID_ARGUMENT, "NULL nonempty input must fail");
    require_empty(result.value);
    require(error[0] != '\0', "invalid input needs a diagnostic");
    require(fastlio_voxel_centroids(nullptr, 0, 1, nullptr, error, sizeof(error))
                == FASTLIO_VOXEL_INVALID_ARGUMENT, "NULL result must fail");
    fastlio_voxel_free_result(nullptr);
    fastlio_voxel_free_result(&result.value);
    fastlio_voxel_free_result(&result.value);
}

void test_invalid_sizes_and_nonfinite_samples() {
    const double invalid_sizes[] = {0, -1, std::numeric_limits<double>::infinity(),
                                    -std::numeric_limits<double>::infinity(),
                                    std::numeric_limits<double>::quiet_NaN()};
    Result result;
    for (const double size : invalid_sizes) {
        require(fastlio_voxel_centroids(nullptr, 0, size, &result.value, nullptr, 0)
                    == FASTLIO_VOXEL_INVALID_ARGUMENT,
                "invalid size must fail even for empty input");
        require_empty(result.value);
    }
    for (const double value : {std::numeric_limits<double>::infinity(),
                              -std::numeric_limits<double>::infinity(),
                              std::numeric_limits<double>::quiet_NaN()}) {
        for (size_t column = 0; column < 4; ++column) {
            std::array<double, 8> points{};
            points[4 + column] = value;
            require(fastlio_voxel_centroids(points.data(), 2, 1, &result.value,
                                           nullptr, 0) == FASTLIO_VOXEL_INVALID_ARGUMENT,
                    "nonfinite XYZ/intensity must fail");
            require_empty(result.value);
        }
    }
}

void test_integer_limits_and_input_count_overflow() {
    Result result;
    for (const double coordinate : {0x1p62, -0x1p62}) {
        const double point[] = {coordinate, 0, 0, 1};
        require(fastlio_voxel_centroids(point, 1, 1, &result.value, nullptr, 0)
                    == FASTLIO_VOXEL_RANGE_ERROR, "integer key boundary must fail");
        require_empty(result.value);
    }
    const double point[] = {1, 0, 0, 1};
    require(fastlio_voxel_centroids(point, 1, std::numeric_limits<double>::denorm_min(),
                                   &result.value, nullptr, 0) == FASTLIO_VOXEL_RANGE_ERROR,
            "division overflow must fail before integer conversion");
    require_empty(result.value);
    require(fastlio_voxel_centroids(point, std::numeric_limits<size_t>::max(), 1,
                                   &result.value, nullptr, 0) == FASTLIO_VOXEL_RANGE_ERROR,
            "impossible point count must fail before reading input");
    require_empty(result.value);
    for (const double coordinate : {std::nextafter(0x1p62, 0.),
                                    std::nextafter(-0x1p62, 0.)}) {
        const double valid_point[] = {coordinate, 0, 0, 1};
        require(fastlio_voxel_centroids(valid_point, 1, 1, &result.value, nullptr, 0)
                    == FASTLIO_VOXEL_OK, "key just inside the limit must succeed");
        require(result.value.counts[0] == 1, "key at inner boundary was lost");
        fastlio_voxel_free_result(&result.value);
    }
}

void test_overflow_failure_leaves_no_result() {
    const double maximum = std::numeric_limits<double>::max();
    const double intensity_overflow[] = {0, 0, 0, maximum, 0, 0, 0, maximum};
    const double coordinate_overflow[] = {maximum, 0, 0, 1, maximum, 0, 0, 1};
    const double output_overflow[] = {-.5, 0, 0, 1, .5, 0, 0, 1e100};
    Result result;
    char error[128]{};
    require(fastlio_voxel_centroids(intensity_overflow, 2, 1, &result.value,
                                   error, sizeof(error)) == FASTLIO_VOXEL_RANGE_ERROR,
            "double intensity sum overflow must fail");
    require_empty(result.value);
    require(fastlio_voxel_centroids(coordinate_overflow, 2, maximum, &result.value,
                                   error, sizeof(error)) == FASTLIO_VOXEL_RANGE_ERROR,
            "double coordinate sum overflow must fail");
    require_empty(result.value);
    require(fastlio_voxel_centroids(output_overflow, 2, 1, &result.value,
                                   error, sizeof(error)) == FASTLIO_VOXEL_RANGE_ERROR,
            "float32 overflow after a valid first output must fail");
    require_empty(result.value);
    require(error[0] != '\0', "range failure needs a diagnostic");
}

void test_error_buffer_is_bounded_and_optional() {
    Result result;
    std::array<char, 8> error{};
    error.fill('x');
    require(fastlio_voxel_centroids(nullptr, 1, 1, &result.value, error.data(), 3)
                == FASTLIO_VOXEL_INVALID_ARGUMENT, "invalid input should fail");
    require(error[2] == '\0', "truncated diagnostic must be NUL-terminated");
    require(error[3] == 'x' && error[7] == 'x', "diagnostic wrote past its capacity");
    error.fill('x');
    fastlio_voxel_centroids(nullptr, 1, 1, &result.value, error.data(), 1);
    require(error[0] == '\0' && error[1] == 'x', "one-byte diagnostic overflowed");
    error.fill('x');
    fastlio_voxel_centroids(nullptr, 1, 1, &result.value, error.data(), 0);
    require(error[0] == 'x', "zero-capacity diagnostic must not be written");
    require(fastlio_voxel_centroids(nullptr, 1, 1, &result.value, nullptr, 100)
                == FASTLIO_VOXEL_INVALID_ARGUMENT, "NULL diagnostic must be optional");
}

void test_repeated_calls_and_release() {
    const double points[] = {-.2, 0, 0, 10, -.8, 0, 0, 30,
                             .1, 0, 0, 20, .9, 0, 0, 40};
    const double invalid[] = {-.5, 0, 0, 1, .5, 0, 0, 1e100};
    Result baseline;
    require(fastlio_voxel_centroids(points, 4, 1, &baseline.value, nullptr, 0)
                == FASTLIO_VOXEL_OK, "baseline must succeed");
    for (int repetition = 0; repetition < 500; ++repetition) {
        Result result;
        require(fastlio_voxel_centroids(points, 4, 1, &result.value, nullptr, 0)
                    == FASTLIO_VOXEL_OK, "repeated call must succeed");
        require(result.value.voxel_count == 2, "repeated voxel count changed");
        require(std::memcmp(result.value.centroids_xyzi, baseline.value.centroids_xyzi,
                            8 * sizeof(float)) == 0, "repeated centroids changed");
        require(std::memcmp(result.value.counts, baseline.value.counts,
                            2 * sizeof(int64_t)) == 0, "repeated occupancy changed");
        fastlio_voxel_free_result(&result.value);
        require_empty(result.value);
        fastlio_voxel_free_result(&result.value);
        require(fastlio_voxel_centroids(invalid, 2, 1, &result.value, nullptr, 0)
                    == FASTLIO_VOXEL_RANGE_ERROR, "repeated failing call must fail");
        require_empty(result.value);
    }
}

}  // namespace

int main() {
    const std::array<std::pair<const char*, void (*)()>, 8> tests{{
        {"centroids_and_order", test_centroids_and_order},
        {"double_boundaries", test_double_boundaries},
        {"empty_and_null_arguments", test_empty_and_null_arguments},
        {"invalid_sizes_and_nonfinite_samples", test_invalid_sizes_and_nonfinite_samples},
        {"integer_limits_and_input_count_overflow", test_integer_limits_and_input_count_overflow},
        {"overflow_failure_leaves_no_result", test_overflow_failure_leaves_no_result},
        {"error_buffer_is_bounded_and_optional", test_error_buffer_is_bounded_and_optional},
        {"repeated_calls_and_release", test_repeated_calls_and_release}}};
    int failures = 0;
    for (const auto& test : tests) {
        try {
            test.second();
            std::cout << "PASS " << test.first << '\n';
        } catch (const std::exception& error) {
            ++failures;
            std::cerr << "FAIL " << test.first << ": " << error.what() << '\n';
        }
    }
    return failures == 0 ? 0 : 1;
}
