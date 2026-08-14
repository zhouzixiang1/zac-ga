import sys

ucz_file = sys.argv[1]
with open(ucz_file, 'r') as f:
    lines = f.readlines()
newlines = []
for line in lines:
    if line.startswith('u3') or line.startswith('u2') or line.startswith('u1'):
        qubits = line.split(' ')[-1]
        newlines.append("rz(pi/2) " + qubits)
    elif not line.startswith('measure'):
        newlines.append(line)

with open(ucz_file.replace('bench_ucz/', 'bench_using/').replace('_ucz.', '.'), 'w') as f:
    f.writelines(newlines)
