cmake_minimum_required(VERSION 3.19...3.28)
project(mqt-qmap-test LANGUAGES C CXX)

include(FetchContent)

# set(FETCHCONTENT_SOURCE_DIR_MQT-CORE ${CMAKE_SOURCE_DIR}/extern/mqt-core)
# add_subdirectory(extern/mqt-qmap)

FetchContent_Declare(
    mqt-qmap
    GIT_REPOSITORY https://github.com/UCLA-VAST/mqt-qmap-hpca25-artifect.git
    GIT_TAG 4a4d7648a65bef0a06009ef9cd9e5fd1e91bfe0b)
FetchContent_MakeAvailable(mqt-qmap)

add_subdirectory(src)