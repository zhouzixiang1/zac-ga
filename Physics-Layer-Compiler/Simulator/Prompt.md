Build a forward neutral-atom quantum circuit simulator GUI, visually similar to the existing Reverse Compiler interface.

Input:
- Directly accept and parse the compiler-generated instruction list.
- Support initialization, parallel atom movement, single-qubit gates, parallel two-qubit gates, and measurement.
- Reuse the existing compiler instruction schema instead of creating a new format.

Hardware layout:
- Storage zone: 40 × 20 sites.
- Entanglement zone: 2 × 20 sites, placed to the right of the storage zone.
- Storage-zone atom indices are assigned column by column, vertically within each column, starting from the column closest to the entanglement zone and continuing toward the farthest column.

Simulation requirements:
- Treat all movements grouped in the same instruction or time step as parallel.
- Animate parallel-moving atoms simultaneously rather than sequentially.
- Preserve atom identities throughout movement.
- Detect and report invalid parallel movement, including destination conflicts, path collisions, atom overlap, and unsupported simultaneous moves.
- Execute parallel gate operations within the same time step simultaneously.
- Maintain the complete atom position and operation state after every instruction.

GUI requirements:
- Left: instruction timeline, with parallel operations grouped into one step.
- Center: animated storage and entanglement zones.
- Right: selected instruction details, atom IDs, start and end coordinates, gate type, parallel group, and validation status.
- Controls: previous, next, play, pause, reset, playback speed, fit view, and jump to step.
- Visually distinguish moving atoms, movement trajectories, active single-qubit gates, and active two-qubit pairs.
- Support exporting the full animation as MP4.

Keep the implementation modular and directly connectable to the compiler output. Prioritize correct parallel execution, atom tracking, collision validation, and clear visualization.