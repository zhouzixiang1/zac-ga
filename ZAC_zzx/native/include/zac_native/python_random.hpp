#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace zac_native {

struct PythonRandomState {
  std::array<std::uint32_t, 624> words{};
  std::size_t index{624};
};

class PythonRandom {
 public:
  explicit PythonRandom(PythonRandomState state);

  std::uint32_t next_u32();
  double random();
  std::uint64_t getrandbits(std::size_t bits);
  std::size_t randbelow(std::size_t stop);
  PythonRandomState state() const noexcept { return state_; }

 private:
  void twist();
  PythonRandomState state_;
};

}  // namespace zac_native
