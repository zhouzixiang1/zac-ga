# Architecture Specification for ZAC

This document describes the JSON-based configuration for defining the hardware architecture specification for the Zoned Architecture Compiler (ZAC). This specification defines the physical and operational parameters of the architecture, including storage and entanglement zones, operational fidelities, and more.

---

## File Structure

The architecture specification JSON file consists of the following key components:

---

### 1. **name**
   - **Description**: Descriptive name for the architecture.
   - **Type**: String
   - **Example**:
     ```json
     "name": "full_compute_store_architecture"
     ```

---

### 2. **operation_duration**
   - **Description**: Specifies the duration (in microseconds) for different operations.
   - **Type**: Object
   - **Keys**:
     - `rydberg`: Duration of Rydberg gates.
     - `1qGate`: Duration of 1qGate gates.
     - `atom_transfer`: Duration of atom transfer.
   - **Example**:
     ```json
     "operation_duration": {
       "rydberg": 0.36,
       "1qGate": 52,
       "atom_transfer": 15
     }
     ```

---

### 3. **operation_fidelity**
   - **Description**: Specifies the fidelity for different operations.
   - **Type**: Object
   - **Keys**:
     - `two_qubit_gate`: Fidelity of two-qubit gates.
     - `single_qubit_gate`: Fidelity of single-qubit gates.
     - `atom_transfer`: Fidelity of atom transfer.
   - **Example**:
     ```json
     "operation_fidelity": {
       "two_qubit_gate": 0.995,
       "single_qubit_gate": 0.9997,
       "atom_transfer": 0.999
     }
     ```

---

### 4. **qubit_spec**
   - **Description**: Specifies the characteristics of qubits.
   - **Type**: Object
   - **Keys**:
     - `T`: Lifetime of qubits (in microseconds).
   - **Example**:
     ```json
     "qubit_spec": {
       "T": 1.5e6
     }
     ```

---

### 5. **storage_zones**
   - **Description**: Defines the storage zones for qubits.
   - **Type**: Array of objects
   - **Keys**:
     - `zone_id`: Unique identifier for the zone.
     - `slms`: Sub-array defining spatial light modulators (SLMs) used.
       - `id`: Identifier for the SLM.
       - `site_seperation`: Array defining the separation between sites in x and y direction.
       - `r`, `c`: Number of rows and columns.
       - `location`: Coordinate of the SLM.
     - `offset`: Offset coordinates for the zone.
     - `dimension`: Dimensions of the zone.
   - **Example**:
     ```json
     "storage_zones": [
       {
         "zone_id": 0,
         "slms": [
           {
             "id": 0,
             "site_seperation": [3, 3],
             "r": 100,
             "c": 100,
             "location": [0, 0]
           }
         ],
         "offset": [0, 0],
         "dimenstion": [300, 300]
       }
     ]
     ```

---

### 6. **entanglement_zones**
   - **Description**: Defines zones for qubit entanglement.
   - **Type**: Array of objects
   - **Structure**: Similar to `storage_zones`.
   - **Example**:
     ```json
     "entanglement_zones": [
       {
         "zone_id": 0,
         "slms": [
           {
             "id": 1,
             "site_seperation": [12, 10],
             "r": 7,
             "c": 20,
             "location": [35, 307]
           }
         ],
         "offset": [35, 307],
         "dimension": [240, 70]
       }
     ]
     ```

---

### 7. **aods**
   - **Description**: Defines the parameters for acousto-optical deflectors (AODs).
   - **Type**: Array of objects
   - **Keys**:
     - `id`: Unique identifier for the AOD.
     - `site_seperation`: Separation between sites.
     - `r`, `c`: Rows and columns.
   - **Example**:
     ```json
     "aods": [
       {
         "id": 0,
         "site_seperation": 2,
         "r": 100,
         "c": 100
       }
     ]
     ```

---

### 8. **arch_range**
   - **Description**: Specifies the range of the architecture in coordinates.
   - **Type**: Array of coordinate pairs.
   - **Example**:
     ```json
     "arch_range": [[0, 0], [297, 402]]
     ```

---

### 9. **rydberg_range**
   - **Description**: Range for Rydberg operations within the architecture.
   - **Type**: Array of coordinate pairs.
   - **Example**:
     ```json
     "rydberg_range": [[[5, 312], [292, 402]]]
     ```

---

## Complete Example

```json
{
  "name": "full_compute_store_architecture",
  "operation_duration": {
    "rydberg": 0.36,
    "1qGate": 52,
    "atom_transfer": 15
  },
  "operation_fidelity": {
    "two_qubit_gate": 0.995,
    "single_qubit_gate": 0.9997,
    "atom_transfer": 0.999
  },
  "qubit_spec": {
    "T": 1.5e6
  },
  "storage_zones": [
    {
      "zone_id": 0,
      "slms": [
        {
          "id": 0,
          "site_seperation": [3, 3],
          "r": 100,
          "c": 100,
          "location": [0, 0]
        }
      ],
      "offset": [0, 0],
      "dimenstion": [300, 300]
    }
  ],
  "entanglement_zones": [
    {
      "zone_id": 0,
      "slms": [
        {
          "id": 1,
          "site_seperation": [12, 10],
          "r": 7,
          "c": 20,
          "location": [35, 307]
        }
      ],
      "offset": [35, 307],
      "dimension": [240, 70]
    }
  ],
  "aods": [
    {
      "id": 0,
      "site_seperation": 2,
      "r": 100,
      "c": 100
    }
  ],
  "arch_range": [[0, 0], [297, 402]],
  "rydberg_range": [[[5, 312], [292, 402]]]
}
```
