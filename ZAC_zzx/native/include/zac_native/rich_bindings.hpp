#pragma once

#include <pybind11/pybind11.h>

namespace zac_native {
void bind_rich_solver(pybind11::module_& module);
}
