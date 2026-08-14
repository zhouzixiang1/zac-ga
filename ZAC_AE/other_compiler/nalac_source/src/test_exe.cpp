#include "Architecture.hpp"
#include "Configuration.hpp"
#include "datastructures/Layer.hpp"
#include "NAMapper.hpp"
#include "QuantumComputation.hpp"
#include "na/NAComputation.hpp"
#include "na/operations/NAGlobalOperation.hpp"
#include "na/operations/NALocalOperation.hpp"
#include "na/operations/NAShuttlingOperation.hpp"

#include <filesystem>
#include <fstream>
#include <ios>
#include <iostream>
#include <ostream>
#include <string>

namespace na {

auto test(const std::string& inputFile, const std::string& architecture,
          const std::string& layout, const std::string& configuration,
          std::ostream& outputStream = std::cout)
    -> void {
  const qc::QuantumComputation qc(inputFile);
  auto arch   = Architecture(architecture, layout);
  auto mapper = NAMapper(arch, Configuration(configuration));
  mapper.map(qc);
  const auto& mappedQc = mapper.getResult();
  outputStream << mappedQc;
}

} // namespace na

struct Options {
  std::string architecture;
  std::string layout;
  std::string configuration;
  std::string inputFilename;
  std::string outputFilename;
};

Options parseCommandLine(int argc, char* argv[]) {
  Options options;

  if (argc == 1 || (argc == 2 && (std::string(argv[1]) == "-h" ||
                                  std::string(argv[1]) == "--help"))) {
    std::cout << "Usage\n\n\t" << argv[0] << " [options]\n" << std::endl;
    std::cout << "Options\n\n\t"
              << "-h, --help\t\t\t"
              << "Show this help message and exit" << std::endl;
    std::cout << "\t"
              << "-a, --architecture <file>\t"
              << "Architecture file (default: config/architecture.json)"
              << std::endl;
    std::cout << "\t"
              << "-l, --layout <file>\t\t"
              << "Layout file (default: config/architecture.csv)" << std::endl;
    std::cout << "\t"
              << "-c, --configuration <file>\t"
              << "Configuration file"
              << " (default: config/configuration.json)" << std::endl;
    std::cout << "\t"
              << "-i, --input <file>\t\t"
              << "Input file" << std::endl;
    std::cout << "\t"
              << "-o, --output <file>\t\t"
              << "Output file (default: stdout)" << std::endl;
    exit(0);
  }

  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];

    if (arg == "-a" || arg == "--architecture") {
      if (i + 1 < argc) {
        options.architecture = argv[++i];
      } else {
        std::cerr << "Error: Missing argument for architecture option"
                  << std::endl;
        exit(1);
      }
    } else if (arg == "-l" || arg == "--layout") {
      if (i + 1 < argc) {
        options.layout = argv[++i];
      } else {
        std::cerr << "Error: Missing argument for layout option" << std::endl;
        exit(1);
      }
    } else if (arg == "-c" || arg == "--configuration") {
      if (i + 1 < argc) {
        options.configuration = argv[++i];
      } else {
        std::cerr << "Error: Missing argument for configuration option"
                  << std::endl;
        exit(1);
      }
    } else if (arg == "-i" || arg == "--input") {
      if (i + 1 < argc) {
        options.inputFilename = argv[++i];
      } else {
        std::cerr << "Error: Missing argument for input filename" << std::endl;
        exit(1);
      }
    } else if (options.inputFilename.empty()) {
      options.inputFilename = argv[i];
    } else if (arg == "-o" || arg == "--output") {
      if (i + 1 < argc) {
        options.outputFilename = argv[++i];
      } else {
        std::cerr << "Error: Missing argument for output filename" << std::endl;
        exit(1);
      }
    } else if (options.outputFilename.empty()) {
      options.outputFilename = argv[i];
    } else {
      std::cerr << "Error: Unknown option or argument: " << arg << std::endl;
      exit(1);
    }
  }
  if (options.inputFilename.empty()) {
    std::cerr << "Error: Missing input filename" << std::endl;
    exit(1);
  }
  return options;
}

auto main(int argc, char* argv[]) -> int {
  Options options = parseCommandLine(argc, argv);
  if (options.architecture.empty()) {
    options.architecture = "config/architecture.json";
  }
  if (options.layout.empty()) {
    options.layout = "config/architecture.csv";
  }
  if (options.configuration.empty()) {
    options.configuration = "config/configuration.json";
  }
  std::ostream* outputStream = &std::cout;
  if (!options.outputFilename.empty()) {
    outputStream = new std::ofstream(options.outputFilename);
    if (!*outputStream) {
      std::cerr << "Error: Could not open output file: "
                << options.outputFilename << std::endl;
      return 1;
    }
  }
  na::test(options.inputFilename, options.architecture, options.layout,
           options.configuration, *outputStream);
  if (outputStream != &std::cout) {
    delete outputStream;
  }
}
