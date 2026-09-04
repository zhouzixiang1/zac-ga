#include "zac_native/python_random.hpp"

#include <limits>
#include <stdexcept>

namespace zac_native {
namespace {
constexpr std::uint32_t kUpperMask = 0x80000000U;
constexpr std::uint32_t kLowerMask = 0x7fffffffU;
constexpr std::uint32_t kMatrixA = 0x9908b0dfU;
}  // namespace

PythonRandom::PythonRandom(PythonRandomState state) : state_(state) {
  if (state_.index > 624) {
    throw std::invalid_argument("Python MT19937 index must be <= 624");
  }
}

void PythonRandom::twist() {
  for (std::size_t index = 0; index < 624; ++index) {
    const auto value = (state_.words[index] & kUpperMask) |
                       (state_.words[(index + 1) % 624] & kLowerMask);
    state_.words[index] = state_.words[(index + 397) % 624] ^ (value >> 1U) ^
                          ((value & 1U) ? kMatrixA : 0U);
  }
  state_.index = 0;
}

std::uint32_t PythonRandom::next_u32() {
  if (state_.index >= 624) twist();
  auto value = state_.words[state_.index++];
  value ^= value >> 11U;
  value ^= (value << 7U) & 0x9d2c5680U;
  value ^= (value << 15U) & 0xefc60000U;
  value ^= value >> 18U;
  return value;
}

double PythonRandom::random() {
  const auto first = static_cast<std::uint64_t>(next_u32() >> 5U);
  const auto second = static_cast<std::uint64_t>(next_u32() >> 6U);
  return (static_cast<double>(first) * 67108864.0 +
          static_cast<double>(second)) /
         9007199254740992.0;
}

std::uint64_t PythonRandom::getrandbits(std::size_t bits) {
  if (bits == 0 || bits > 64) {
    throw std::invalid_argument("native getrandbits supports 1..64 bits");
  }
  if (bits <= 32) {
    return static_cast<std::uint64_t>(next_u32() >> (32U - bits));
  }
  const auto low = static_cast<std::uint64_t>(next_u32());
  const auto remaining = bits - 32;
  const auto high = static_cast<std::uint64_t>(
      next_u32() >> (32U - remaining));
  return low | (high << 32U);
}

std::size_t PythonRandom::randbelow(std::size_t stop) {
  if (stop == 0) throw std::invalid_argument("randbelow stop must be positive");
  std::size_t bits = 0;
  for (auto value = stop; value != 0; value >>= 1U) ++bits;
  std::uint64_t candidate;
  do {
    candidate = getrandbits(bits);
  } while (candidate >= stop);
  return static_cast<std::size_t>(candidate);
}

}  // namespace zac_native
