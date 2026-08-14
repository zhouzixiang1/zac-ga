# !/bin/bash

echo "[INFO] Artifact Evaluation for ZAC: Data collection"

echo "[INFO] Collecting data for superconducting qubit platform, including grid and heavy-hexagon architectures"
START=$(date +%s)
python3 other_compiler/superconducting/sabre.py exp_setting/comparison_sc.json &> log/sc.log
TOOL_END=$(date +%s)
TOOL_RUNTIME=$((TOOL_END - START))
echo "[INFO] Done using $TOOL_RUNTIME seconds."

echo "[INFO] Collecting data for monolithic neutral atom architecture using Enola"
TOOL_START=$(date +%s)
python3 other_compiler/enola/run_enola.py exp_setting/comparison_enola.json &> log/enola.log
TOOL_END=$(date +%s)
TOOL_RUNTIME=$((TOOL_END - START))
echo "[INFO] Done using $TOOL_RUNTIME seconds."

echo "[INFO] Collecting data for monolithic neutral atom architecture using Atomique"
cd other_compiler/atomique
TOOL_START=$(date +%s)
./run_atomique.sh
cd ../..
mv atomique.log log/
TOOL_END=$(date +%s)
TOOL_RUNTIME=$((TOOL_END - TOOL_START))
echo "[INFO] Done using $TOOL_RUNTIME seconds."

echo "[INFO] Collecting data for zoned neutral atom architecture using ZAC"
echo "[INFO] Experiment 1: ablation study"
TOOL_START=$(date +%s)
python3 run.py exp_setting/comparison_technique.json &> log/zac_comparison_technique.log
TOOL_END=$(date +%s)
TOOL_RUNTIME=$((TOOL_END - TOOL_START))
echo "[INFO] Done using $TOOL_RUNTIME seconds."

echo "[INFO] Experiment 2: AOD number comparison"
TOOL_START=$(date +%s)
python3 run.py exp_setting/comparison_arch_aod.json  &> log/zac_comparison_arch_aod.log
TOOL_END=$(date +%s)
TOOL_RUNTIME=$((TOOL_END - TOOL_START))
echo "[INFO] Done using $TOOL_RUNTIME seconds."

echo "[INFO] Experiment 3: Zoned architecture comparison"
TOOL_START=$(date +%s)
python3 run.py exp_setting/comparison_arch_zone.json &> log/zac_comparison_arch_zone.log
TOOL_END=$(date +%s)
TOOL_RUNTIME=$((TOOL_END - TOOL_START))
echo "[INFO] Done using $TOOL_RUNTIME seconds."

TOTAL_RUNTIME=$((TOOL_END - START))
echo "[INFO] All experiements are done using $TOTAL_RUNTIME seconds"