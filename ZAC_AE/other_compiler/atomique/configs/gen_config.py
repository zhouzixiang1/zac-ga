import numpy as np

if __name__ == "__main__":

    n_qubit_per_array = 49
    n_rows = []
    n_cols = []
    for i in range(n_qubit_per_array, 0, -1):
        n_col = int(n_qubit_per_array // i)
        if len(n_cols) == 0 or n_col != n_cols[-1]:
            n_rows.append(i)
            n_cols.append(n_col)

    for nr in n_rows:
        print(f"- {nr}")
    print("==")
    for nl in n_cols:
        print(f"- {nl}")
