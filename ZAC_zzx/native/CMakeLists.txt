cmake_minimum_required(VERSION 3.20)
project(zac_native VERSION 0.3.0 LANGUAGES CXX)

find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

add_library(zac_native_lib STATIC
  src/core.cpp
  src/python_random.cpp
  src/rich_solver.cpp)
target_include_directories(zac_native_lib PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_features(zac_native_lib PUBLIC cxx_std_17)
set_target_properties(zac_native_lib PROPERTIES POSITION_INDEPENDENT_CODE ON)

if(MSVC)
  target_compile_options(zac_native_lib PRIVATE /W4 /permissive-)
else()
  target_compile_options(zac_native_lib PRIVATE -Wall -Wextra -Wpedantic)
endif()

pybind11_add_module(zac_native_core
  bindings/pybind_module.cpp
  bindings/rich_bindings.cpp)
target_link_libraries(zac_native_core PRIVATE zac_native_lib)
target_compile_features(zac_native_core PRIVATE cxx_std_17)
target_compile_definitions(zac_native_core PRIVATE VERSION_INFO="${PROJECT_VERSION}")
target_compile_definitions(zac_native_core PRIVATE
  ZAC_CXX_COMPILER_ID="${CMAKE_CXX_COMPILER_ID}"
  ZAC_CXX_COMPILER_VERSION="${CMAKE_CXX_COMPILER_VERSION}"
  ZAC_BUILD_TYPE="${CMAKE_BUILD_TYPE}")
install(TARGETS zac_native_core LIBRARY DESTINATION . RUNTIME DESTINATION .)

include(CTest)
if(BUILD_TESTING)
  add_executable(zac_native_tests tests/cpp/native_tests.cpp)
  target_link_libraries(zac_native_tests PRIVATE zac_native_lib)
  target_compile_features(zac_native_tests PRIVATE cxx_std_17)
  add_test(NAME zac_native_tests COMMAND zac_native_tests)
  add_executable(zac_rich_solver_tests tests/cpp/rich_solver_tests.cpp)
  target_link_libraries(zac_rich_solver_tests PRIVATE zac_native_lib)
  target_compile_features(zac_rich_solver_tests PRIVATE cxx_std_17)
  add_test(NAME zac_rich_solver_tests COMMAND zac_rich_solver_tests)
endif()
