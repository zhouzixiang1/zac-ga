from process_data.plot_figure import plot_figure
from process_data.write_csv import write_qasm_csv, write_runtime_csv


if __name__ == "__main__":
    for generate_data in ["fidelity_breakdown", "technique_comp", "arch_comp", "aod_comp", "technique_comp_time"]:
        if generate_data == "fidelity_breakdown":
            working_directory = ["result/atomique", "result/enola/fidelity", \
                                "result/nalac/fidelity/",  
                                "result/zac/tech_eval/zac/fidelity"]
            compiler_name = ["Atomique", "Enola", "NALAC", "ZAC"]
            list_color = None
        elif generate_data == "technique_comp":
            working_directory = ["result/zac/tech_eval/vanilla/fidelity",\
                                "result/zac/tech_eval/dynPlace/fidelity",\
                                "result/zac/tech_eval/dynPlace_reuse/fidelity",\
                                "result/zac/tech_eval/zac/fidelity"]
            compiler_name = ["Vanilla", "dynPlace", "dynPlace+reuse", "SA+dynPlace+reuse"]
            list_color = ['forestgreen', 'lightsalmon', 'grey', "royalblue"]
        elif generate_data == "arch_comp":
            working_directory = ["result/sc/eagle", "result/sc/grid", "result/atomique", \
                                "result/enola/fidelity", "result/nalac/fidelity/", \
                                "result/zac/tech_eval/zac/fidelity"]
            compiler_name = ["SC-Heron", "SC-Grid", "Monolithic-Atomique", "Monolithic-Enola", "Zoned-NALAC", "Zoned-ZAC"]
            list_color = [ "grey", "mediumpurple", 'forestgreen', 'lightsalmon', "palevioletred", "royalblue", "grey", "mediumpurple"]
        elif generate_data == "aod_comp":
            working_directory = ["result/zac/tech_eval/zac/fidelity",\
                                "result/zac/arch_eval/2aod/fidelity", \
                                "result/zac/arch_eval/3aod/fidelity", \
                                "result/zac/arch_eval/4aod/fidelity"]
            compiler_name = ["1AOD", "2AOD", "3AOD", "4AOD"]
            list_color = ['forestgreen', 'lightsalmon', 'grey', "royalblue"]
        elif generate_data == "technique_comp_time":
            working_directory = [["result/atomique", \
                                "result/enola/time", "result_static/nalac/",
                                "result/zac/tech_eval/vanilla/time",\
                                "result/zac/tech_eval/dynPlace/time",\
                                "result/zac/tech_eval/dynPlace_reuse/time",\
                                "result/zac/tech_eval/zac/time"],
                                ["result/atomique", \
                                "result/enola/fidelity", "result_static/nalac/fidelity",
                                "result/zac/tech_eval/vanilla/fidelity",\
                                "result/zac/tech_eval/dynPlace/fidelity",\
                                "result/zac/tech_eval/dynPlace_reuse/fidelity",\
                                "result/zac/tech_eval/zac/fidelity"]]
            compiler_name = ["Atomique", 'Enola', 'NALAC',
                            "ZAC-Vanilla", "ZAC-dynPlace", "ZAC-dynPlace+reuse", "ZAC-SA+dynPlace+reuse"]
            list_color = ['forestgreen', 'lightsalmon', "palevioletred", "royalblue", "royalblue", "royalblue", "royalblue", "royalblue"]
        
        if generate_data == "technique_comp_time":
            filename = write_runtime_csv(working_directory, compiler_name)
        else:
            filename = write_qasm_csv(generate_data, working_directory, compiler_name)
        
        plot_figure(generate_data, filename, compiler_name, list_color)


