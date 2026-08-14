# Experimental Configuration for ZAC

This document provides a tutorial for configuring experimental settings for ZAC using a JSON-based specification. The configuration file allows you to define various parameters for ZAC.

---

## File Structure

A JSON configuration file for our tool includes the following top-level keys:

### 1. **qasm_list**
   - **Description**: A list of file paths to QASM files that define the quantum circuits to be used in the experiment. If the provided path is a directory, then we will iterate through all the QASM files in the directory.
   - **Type**: Array of strings
   - **Example**:
     ```json
     "qasm_list": ["benchmark/iqp.qasm"]
     ```

---

### 2. **zac_setting**
   - **Description**: An array of configuration objects for zoned architecture settings. Each object defines the architecture and execution settings.
   - **Type**: Array of objects
   - **Structure**:
     - `arch_spec` (string): Path to the architecture specification file.
     - `dependency` (boolean): Whether to consider gate dependencies. With gate dependency, we apply as-soon-as-possible (ASAP) scheduling, while without gate dependency, we apply the edge coloring algorithm for scheduling.
     - `dir` (string): Directory to save results.
     - `routing_strategy` (string): Strategy for routing 
        - `"mis"`: solve maximum independent set
        - `"maximalis"`: solve maximal independent set
        - `"maximalis_sort"`: solve maximal independent set with modified heuristic
     - `scheduling` (string): Scheduling method (e.g., `"asap"`).
     - `trivial_placement` (boolean): Use trivial initial placement by placing qubits according to their index.
     - `dynamic_placement` (boolean): Enable dynamic placement during execution.
     - `use_window` (boolean): Enforce window optimization.
     - `window_size` (integer): Vertex number limit in solving maximal independent set.
     - `reuse` (boolean): Enable qubit reuse.
     - `use_verifier` (boolean): Use a verifier for result validation.
   - **Example**:
     ```json
     "zac_setting": [
       {
         "arch_spec": "hardware_spec/logic_architecture.json",
         "dependency": true,
         "dir": "result/zac/logic/",
         "routing_strategy": "maximalis_sort",
         "scheduling": "asap",
         "trivial_placement": false,
         "dynamic_placement": true,
         "use_window": true,
         "window_size": 1000,
         "reuse": true,
         "use_verifier": true
       }
     ]
     ```

---

### 3. **simulation**
   - **Description**: Flag to enable or disable simulation.
   - **Type**: Boolean
   - **Example**:
     ```json
     "simulation": true
     ```

---

### 4. **animation**
   - **Description**: Flag to enable or disable animation for visualization purposes.
   - **Type**: Boolean
   - **Example**:
     ```json
     "animation": true
     ```

---

## Example Configuration

Here is a complete example JSON file:

```json
{
  "qasm_list": ["benchmark/iqp.qasm"],
  "zac_setting": [
    {
      "arch_spec": "hardware_spec/logic_architecture.json",
      "dependency": true,
      "dir": "result/zac/logic/",
      "routing_strategy": "maximalis_sort",
      "scheduling": "asap",
      "trivial_placement": false,
      "dynamic_placement": true,
      "use_window": true,
      "window_size": 1000,
      "reuse": true,
      "use_verifier": true
    }
  ],
  "simulation": true,
  "animation": true
}
```

---

## Instructions for Use

1. Create a JSON file using the structure defined above.
2. Adjust the parameters to fit your experimental requirements.
3. Save the file and provide its path when running `run.py`.

