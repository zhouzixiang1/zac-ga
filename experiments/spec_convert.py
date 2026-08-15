"""Convert ZAC architecture spec JSON to MQT-QMAP zoned architecture JSON.

Field mapping (verified against qmap-main/eval/na/zoned/square_architecture.json):
  operation_duration.rydberg   -> rydberg_gate
  operation_duration.1qGate    -> single_qubit_gate
  operation_fidelity.two_qubit_gate -> rydberg_gate
  site_seperation (ZAC typo)   -> site_separation
  dimenstion  (ZAC typo)       -> dimension
Everything else (zones/slms/aods/arch_range/rydberg_range) is structurally identical.
"""
import json
import sys


def convert(zac_spec: dict) -> dict:
    spec = json.loads(json.dumps(zac_spec))  # deep copy
    od = spec.get("operation_duration", {})
    if "rydberg" in od:
        od["rydberg_gate"] = od.pop("rydberg")
    if "1qGate" in od:
        od["single_qubit_gate"] = od.pop("1qGate")
    of = spec.get("operation_fidelity", {})
    if "two_qubit_gate" in of:
        of["rydberg_gate"] = of.pop("two_qubit_gate")
    if "single_qubit_gate" not in of and "1qGate" in of:
        of["single_qubit_gate"] = of.pop("1qGate")
    for zone_key in ("storage_zones", "entanglement_zones"):
        for zone in spec.get(zone_key, []):
            if "dimenstion" in zone:
                zone["dimension"] = zone.pop("dimenstion")
            for slm in zone.get("slms", []):
                if "site_seperation" in slm:
                    slm["site_separation"] = slm.pop("site_seperation")
    for aod in spec.get("aods", []):
        if "site_seperation" in aod:
            aod["site_separation"] = aod.pop("site_seperation")
    return spec


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        spec = convert(json.load(f))
    with open(dst, "w") as f:
        json.dump(spec, f, indent=2)
    print(f"converted {src} -> {dst}")


if __name__ == "__main__":
    main()
