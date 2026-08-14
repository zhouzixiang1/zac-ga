import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import matplotlib
import matplotlib.font_manager as font_manager
from matplotlib.patches import Rectangle
from scipy.stats import gmean

font_dirs = ['font/', ]
font_files = font_manager.findSystemFonts(fontpaths=font_dirs)
for font_file in font_files:
    font_manager.fontManager.addfont(font_file)
matplotlib.rcParams['font.family'] = 'Linux Libertine O'
matplotlib.rcParams.update({'font.size': 8})

fidelity_threshold = 0.049

our_name = "ZAC"
# for paper
def fidelity_breakdown_qasm_motivation(filename, list_data_name):
    # Read the CSV file
    data = pd.read_csv(filename, header=[0,1])
    data = data[data[list_data_name[-1]]["cir_fidelity"] >= 0.01]
    data = data[(data["filename"]["x"] != "ising_n98") & (data["filename"]["x"] != "ising_n42")]
    index = data["filename"]["x"]

    list_color = ['forestgreen', 'royalblue', 'lightsalmon', 'grey']
    # list_color = [['forestgreen', 'limegreen', 'lime'],
    #                 ['peru', 'darkorange', 'lightsalmon'],
    #                ['royalblue', 'steelblue', 'deepskyblue']]

    # Define the index array for the bars
    plt.figure(figsize=(3.5, 1.5))
    list_bar = []
    bar_width = 2
    for i, data_name in enumerate(list_data_name):
        location = [5 * j+ i*bar_width for j in range(len(index))]

        list_fidelity_terms = ["cir_fidelity_2q_gate", \
                                "cir_fidelity_2q_gate_for_idle", "cir_fidelity_atom_transfer", \
                                "cir_fidelity_coherence"]
        
        list_data = []
        for term in list_fidelity_terms:
            tmp = data[data_name][term]
            log_tmp = np.log10(tmp)
            list_data.append(log_tmp)

        # Plot the bars     
        bar = plt.bar(location, list_data[0], width=bar_width, color=list_color[0], label='2Q gate')
        bar = plt.bar(location, list_data[1], bottom=list_data[0], width=bar_width, color=list_color[1], label='Excitation error')
        tmp = list_data[0]+list_data[1]
        # list_bar.append(bar)
        bar = plt.bar(location, list_data[2], bottom=tmp, width=bar_width, color=list_color[2], label='atom transfer')
        # list_bar.append(bar)
        tmp = tmp + list_data[2]
        bar = plt.bar(location, list_data[3], bottom=tmp, width=bar_width, color=list_color[3], label='decoherence')
        # list_bar.append(bar)
        

        # Add labels and title
    plt.ylabel('fidelity, log scale')
    plt.title('error breakdown for monolithic architecture', fontsize=9)
    plt.xticks(location, labels=index.tolist(), rotation=30, ha='right')
    plt.yticks([0, -1, -2], labels=['1', '0.1', '0.01'])
    plt.ylim(-2.5, 0)

    #Create legend
    plt.legend(loc="center left", bbox_to_anchor=(1, 0.5))

    # Show plot
    plt.savefig('fig/fidelity_breakdown.pdf', bbox_inches='tight')

def fidelity_breakdown_qasm(filename, list_data_name):
    # Read the CSV file
    data = pd.read_csv(filename, header=[0,1])
    data = data[data[list_data_name[-1]]["cir_fidelity"] >= fidelity_threshold]
    index = data["filename"]["x"]

    # list_color = ['green', 'royalblue', 'lightsalmon']
    list_color = [['forestgreen', 'limegreen', 'lime'],
                    ['peru', 'darkorange', 'lightsalmon'],
                   ['royalblue', 'steelblue', 'deepskyblue']]

    # Define the index array for the bars
    plt.figure(figsize=(4.6, 2.4))
    list_bar = []
    bar_width = 5 / (len(list_data_name) + 1)
    for i, (data_name, color) in enumerate(zip(list_data_name, list_color)):
        location = [5 * j+ i*bar_width for j in range(len(index))]

        list_fidelity_terms = ["cir_fidelity_2q_gate", \
                                "cir_fidelity_2q_gate_for_idle", "cir_fidelity_atom_transfer", \
                                "cir_fidelity_coherence"]
        
        list_data = []
        for term in list_fidelity_terms:
            tmp = data[data_name][term]
            log_tmp = np.log10(tmp)
            list_data.append(log_tmp)

        # Plot the bars     
        tmp = list_data[0]+list_data[1]
        bar = plt.bar(location, tmp, width=bar_width, color=color[0], label='2Q gate')
        list_bar.append(bar)
        bar = plt.bar(location, list_data[2], bottom=tmp, width=bar_width, color=color[1], label='atom transfer')
        list_bar.append(bar)
        tmp = tmp + list_data[2]
        bar = plt.bar(location, list_data[3], bottom=tmp, width=bar_width, color=color[2], label='decoherence')
        list_bar.append(bar)
        

        # Add labels and title
    plt.ylabel('fidelity, log scale')
    plt.title('error breakdown', fontsize=9)
    plt.xticks(location, labels=index.tolist(), rotation=30, ha='right')
    plt.yticks([0, -1, -2], labels=['1', '0.1', '0.01'])
    plt.ylim(-2.5, 0)

    # create blank rectangle
    extra = Rectangle((0, 0), 0.5, 0.5, fc="w", fill=False, edgecolor='none', linewidth=0)

    #Create organized list containing all handles for table. Extra represent empty space
    legend_handle = [extra, extra, extra, extra, extra, list_bar[0], list_bar[1], list_bar[2], extra, list_bar[3], list_bar[4], list_bar[5], extra, list_bar[6], list_bar[7], list_bar[8]]
    # legend_handle = [extra, extra, extra, extra, extra, list_bar[0], list_bar[1], list_bar[2], extra, list_bar[3], list_bar[4], list_bar[5], extra, list_bar[0], list_bar[1], list_bar[2]]
    

    #Define the labels
    label_col_1 = ["", "2Q gate", "atom transfer", "decoherence"]
    label_j_1 = [list_data_name[0]]
    label_j_2 = [list_data_name[1]]
    label_j_3 = [list_data_name[2]]
    label_empty = [""]

    #organize labels for table construction
    legend_labels = np.concatenate([label_col_1, label_j_1, label_empty * 3, label_j_2, label_empty * 3, label_j_3, label_empty * 3])
    # legend_labels = np.concatenate([label_col_1, label_j_1, label_empty * 3, label_j_2, label_empty * 3])

    #Create legend
    plt.legend(legend_handle, legend_labels, 
            loc = 9, ncol = 4, shadow = True, handletextpad = -2, bbox_to_anchor=(0.45,-0.4))


    # Show plot
    plt.savefig('fig/fidelity_breakdown.pdf', bbox_inches='tight')

def fidelity_term_comparison_qasm(filename, list_data_name, data_type):
    # Read the CSV file
    data = pd.read_csv(filename, header=[0,1])
    data = data[data[our_name]["cir_fidelity"] >= fidelity_threshold]
    index = data["filename"]["x"].tolist()
    index.append("GMean")

    # list_color = ['green', 'royalblue', 'lightsalmon']
    list_color = ['forestgreen', 'lightsalmon', 'palevioletred', 'royalblue']

    # Define the index array for the bars
    list_fidelity_terms = ["cir_fidelity_2q_gate", \
                                "cir_fidelity_2q_gate_for_idle", "cir_fidelity_atom_transfer", \
                                "cir_fidelity_coherence"]
    
    if data_type == "ideal":
        list_fidelity_term = ["decoherence"]
    else:
        list_fidelity_term = ["2Q gate", "atom transfer", "decoherence"]

    bar_width = 1 / (len(list_data_name) + 1)

    fig, axs = plt.subplots(3, figsize=(7.25, 1.1* 3), sharex=True, sharey=False)
    two_qubit_fidelity_ref = data[list_data_name[-1]][list_fidelity_terms[0]] * data[list_data_name[-1]][list_fidelity_terms[1]]
    for term, ax in zip(list_fidelity_term, axs):
        for i, (data_name, color) in enumerate(zip(list_data_name, list_color)):
            # print(data_name)
            if term == "2Q gate":
                tmp = data[data_name][list_fidelity_terms[0]]
                log_tmp = np.log10(tmp)
                bar_data = log_tmp
                # print(tmp)
                tmp = data[data_name][list_fidelity_terms[1]]
                # print(tmp)
                log_tmp = np.log10(tmp)
                bar_data += log_tmp
                bar_data = bar_data.tolist()
                geomean = np.exp(np.log(data[data_name][list_fidelity_terms[0]] * data[data_name][list_fidelity_terms[1]]).mean())
                bar_data.append( np.log10(geomean) )
                # print(bar_data)
                # print(term)
                improve_ratio = two_qubit_fidelity_ref / (data[data_name][list_fidelity_terms[0]] * data[data_name][list_fidelity_terms[1]])
                # print("{}/{}".format(list_data_name[len(list_data_name) - 1], list_data_name[i]))
                # print(improve_ratio)
                # print(np.exp(np.log(improve_ratio).mean()))
            elif term == "atom transfer":
                tmp = data[data_name][list_fidelity_terms[2]]
                log_tmp = np.log10(tmp)
                bar_data = log_tmp
                bar_data = bar_data.tolist()
                geomean = np.exp(np.log(tmp).mean())
                bar_data.append( np.log10(geomean) )
                # print(term)
                improve_ratio = data[list_data_name[-1]][list_fidelity_terms[2]]  / data[data_name][list_fidelity_terms[2]]
                # print("{}/{}".format(list_data_name[len(list_data_name) - 1], list_data_name[i]))
                # print(improve_ratio)
                # print(np.exp(np.log(improve_ratio).mean()))
            else:
                tmp = data[data_name][list_fidelity_terms[3]]
                log_tmp = np.log10(tmp)
                bar_data = log_tmp
                bar_data = bar_data.tolist()
                geomean = np.exp(np.log(tmp).mean())
                bar_data.append( np.log10(geomean) )
                # print(term)
                improve_ratio = data[list_data_name[-1]][list_fidelity_terms[3]]  / data[data_name][list_fidelity_terms[3]]
                # print("{}/{}".format(list_data_name[len(list_data_name) - 1], list_data_name[i]))
                # print(improve_ratio)
                # print(np.exp(np.log(improve_ratio).mean()))
            location = [1 * j+ (i - 1.5)*bar_width for j in range(len(index))]
            # Plot the bars     
            ax.bar(location, bar_data, width=bar_width, color=color, label=data_name)
            


        # Add labels and title
        ax.set_title("{} comparison".format(term), fontsize=9)
        
        if term == "2Q gate":
            ax.set_yticks([0, -1, -2], labels=['1', '0.1', '0.01'])
            ax.set_ylim(-2.5, 0)
        elif term == "decoherence":
            ax.set_yticks([0, np.log10(0.5), np.log10(0.3)], labels=['1', '0.5', '0.25'])
            ax.set_ylim(np.log10(0.2), 0)
        else:
            ax.set_yticks([0, np.log10(0.85), np.log10(0.7)], labels=['1', '0.85', '0.7'])
            ax.set_ylim(np.log10(0.55), 0)

    axs[1].set_ylabel('fidelity, log scale'.format(term))
    location = [1 * j+ (1 - 1.5)*bar_width for j in range(len(index))]
    axs[2].set_xticks(location, labels=index, rotation=15, ha='right')
    # axs[2].legend(loc='upper center', ncol=3,
    #          bbox_to_anchor=(0.5, -0.7))
    axs[1].legend(loc='center left', bbox_to_anchor=(1, 0.5))
    fig.tight_layout()
    plt.savefig('fig/fig_9-fidelity_breakdown.pdf'.format(term), bbox_inches='tight')
        
def comparison_dependency_technique(filename, list_data_name, title, fig_filename, list_color):
    # Read the CSV file
    data = pd.read_csv(filename,header=[0,1])
    data = data[data[list_data_name[-1]]["cir_fidelity"] >= fidelity_threshold]
    # data = data[data[list_data_name[-1]]["cir_fidelity"] > 0.01 ]
    # Define the index array for the bars
    index = data["filename"]["x"].tolist()
    if fig_filename != "fig/comparison-zone.pdf":
        index.append("GMean")
    
    bar_width = 4 / (len(list_data_name) + 1)
    if fig_filename == "fig/comparison-arch.pdf":
        plt.figure(figsize=(8, 2.3))    
    elif fig_filename == "fig/comparison-zone.pdf":
        plt.figure(figsize=(4, 2.3))    
    else:
        plt.figure(figsize=(8, 1.7))
    for i, (data_name, color) in enumerate(zip(list_data_name, list_color)):
        location = [4 * j+ (i - 1.5)*bar_width for j in range(len(index))]
        # if data_name == our_name or data_name == "Zoned-based Arch-Ours": 
        #     color = 'royalblue'
        tmp_list = data[data_name]["cir_fidelity"].tolist()
        if fig_filename != "fig/comparison-zone.pdf":
            geomean = gmean(data[data_name]["cir_fidelity"])
            # geomean = np.exp(np.log(data[data_name]["cir_fidelity"]).mean())
            tmp_list.append(geomean)
        plt.bar(location, tmp_list, width=bar_width, color=color, label=data_name)
        # if fig_filename == "fig/comparison-arch.pdf" or fig_filename == "fig/comparison-ideal.pdf":
        #     improve_ratio = data[list_data_name[-1]]["cir_fidelity"]/data[list_data_name[i]]["cir_fidelity"]
        #     # print("{}/{}".format(list_data_name[-1], list_data_name[i]))
        #     # print(improve_ratio)
        #     # print(gmean(improve_ratio))
        #     # print(np.exp(np.log(improve_ratio).mean()))
        # elif fig_filename == "fig/comparison-ideal.pdf":
        #     # print("{}/{}".format(list_data_name[i], list_data_name[-1]))
        #     improve_ratio = data[list_data_name[i]]["cir_fidelity"]/data[list_data_name[-1]]["cir_fidelity"]
        #     # print(gmean(improve_ratio))
        #     # print(np.exp(np.log(improve_ratio).mean()))
        # else:
        #     if i > 0:
        #         improve_ratio = data[list_data_name[i]]["cir_fidelity"]/data[list_data_name[i-1]]["cir_fidelity"]
                # print("{}/{}".format(list_data_name[i], list_data_name[i-1]))
                # print(improve_ratio)
                # print(max(improve_ratio))
                # print(gmean(improve_ratio))
                # print(np.exp(np.log(improve_ratio).mean()))
        
    plt.ylabel('fidelity')
    # plt.ylim(0, 0.65)
    plt.title(title, fontsize=9)
    location = [4 * j+ (len(list_data_name)//2 - 1.5)*bar_width for j in range(len(index))]
    plt.xticks(location, labels=index, rotation=15, ha='right')
    # Add legend
    if fig_filename == "fig/comparison-arch.pdf":
        plt.legend(loc='upper center', ncol=6,
             bbox_to_anchor=(0.5, -0.4))
    else:
        plt.legend(loc="center left", bbox_to_anchor=(1, 0.5))
    plt.tight_layout()

    # Show plot
    # plt.savefig('fig/comparison-d/ependency.pdf', bbox_inches='tight')
    plt.savefig(fig_filename, bbox_inches='tight')

def comparison_duration(filename, list_data_name):
    # Read the CSV file
    data = pd.read_csv(filename, header=[0,1])
    data = data[data[our_name]["cir_fidelity"] >= fidelity_threshold]

    # list_color = ['green', 'royalblue', 'lightsalmon']
    list_color = ['forestgreen', 'lightsalmon', 'palevioletred', 'royalblue']
    # data = data[data[list_data_name[-1]]["cir_fidelity"] > 0.01 ]
    # Define the index array for the bars
    index = data["filename"]["x"].tolist()
    
    bar_width = 1 / (len(list_data_name) + 1)
    plt.figure(figsize=(8, 1.5))    
    # print("duration")
    for i, (data_name, color) in enumerate(zip(list_data_name, list_color)):
        tmp_list = data[data_name]["cir_duration"].tolist()
        tmp_list = [i / 1000 for i in tmp_list]

        location = [1 * j+ (i - 1.5)*bar_width for j in range(len(index))]
        # if data_name == our_name or data_name == "Zoned-based Arch-Ours": 
        #     color = 'royalblue'
        plt.bar(location, tmp_list, width=bar_width, color=color, label=data_name)
        improve_ratio = data[list_data_name[-1]]["cir_duration"]/data[list_data_name[i]]["cir_duration"]
        # print("{}/{}".format(list_data_name[-1], list_data_name[i]))
        # print(gmean(improve_ratio))
        
    plt.ylabel('duration (ms)')
    # plt.ylim(0, 0.65)
    plt.title("circuit duration comparison", fontsize=9)
    location = [1 * j+ (1 - 1.5)*bar_width for j in range(len(index))]
    plt.xticks(location, labels=index, rotation=15, ha='right')
    # Add legend
    plt.legend(loc="center left", bbox_to_anchor=(1, 0.5))
    plt.tight_layout()

    # Show plot
    # plt.savefig('fig/comparison-d/ependency.pdf', bbox_inches='tight')
    plt.savefig("fig/fig_10-comparison_duration.pdf", bbox_inches='tight')

    plt.figure(figsize=(8, 1.5))       

    for i, (data_name, color) in enumerate(zip(list_data_name, list_color)):
        tmp_list = data[data_name]["cir_duration"].tolist()

        location = [1 * j+ (i - 1.5)*bar_width for j in range(len(index) + 1)]
        # if data_name == our_name or data_name == "Zoned-based Arch-Ours": 
        #     color = 'royalblue'
        improve_ratio = (data[list_data_name[i]]["cir_duration"]/data[list_data_name[-1]]["cir_duration"]).tolist()
        improve_ratio.append(gmean(improve_ratio))
        plt.bar(location, improve_ratio, width=bar_width, color=color, label=data_name)
        
    plt.ylabel('ratio')
    # plt.ylim(0, 0.65)
    plt.title("circuit duration comparison", fontsize=9)
    location = [1 * j+ (1 - 1.5)*bar_width for j in range(len(index) + 1)]
    index.append("GMean")
    plt.xticks(location, labels=index, rotation=15, ha='right')
    # Add legend
    plt.legend(loc="center left", bbox_to_anchor=(1, 0.5))
    plt.tight_layout()

    # Show plot
    # plt.savefig('fig/comparison-d/ependency.pdf', bbox_inches='tight')
    plt.savefig("fig/comparison-duration-ratio.pdf", bbox_inches='tight')

def comparison_dependency_technique_time(filename, list_data_name, list_color):
    # Read the CSV file
    data = pd.read_csv(filename, header=[0,1])
    index = data["filename"]["x"].tolist()
    index.append("Average")

    bar_width = 1 / (len(list_data_name) + 1)
    plt.figure(figsize=(3.55, 2))    

    list_marker = ['*', '*', '*', '.', 's', '^', '*']

    for i, (data_name, color, marker) in enumerate(zip(list_data_name, list_color, list_marker)):
        tmp_list = data[data_name]["total"].tolist()
        average_runtime = gmean(tmp_list)
        tmp_list = data[data_name]["cir_fidelity"].tolist()
        gmean_fidelity = gmean(tmp_list)
        plt.plot(average_runtime, gmean_fidelity, color=color, marker=marker, label=data_name, linestyle='None')
        improve_ratio = data[list_data_name[i]]["total"]/data[list_data_name[-2]]["total"]
        # print("{}/{}".format(list_data_name[i], list_data_name[-2]))
        # print(mean(improve_ratio))
        improve_ratio = data[list_data_name[-2]]["cir_fidelity"]/data[list_data_name[i]]["cir_fidelity"]
        # print("{}/{}".format(list_data_name[-2], list_data_name[i]))
        # print(gmean(improve_ratio))
        # if data_name == "NALAC":
        #     plt.plot(0, 0, color="white", marker=marker, label="ZAC", linestyle='None')
        
    plt.xlabel('time (s), log scale')
    plt.xscale('log')
    plt.ylabel('fidelity')
    # plt.yscale('log')
    # plt.ylim(0, 0.65)
    plt.title("compilation time and fidelity", fontsize=9)
    # Add legend
    plt.legend(loc="center left", bbox_to_anchor=(1, 0.5))
    plt.tight_layout()

    # Show plot
    # plt.savefig('fig/comparison-d/ependency.pdf', bbox_inches='tight')
    plt.savefig("fig/fig_12-comparison_compilation_time.pdf", bbox_inches='tight')

def plot_figure(generate_data, filename, list_data_name, list_color):
    if generate_data == "fidelity_breakdown":
        # fidelity_breakdown_qasm(filename, list_data_name)
        fidelity_term_comparison_qasm(filename, list_data_name, generate_data)
        comparison_duration(filename, list_data_name)
        # comparison_dependency(filename, list_data_name, generate_data)
    
    elif generate_data == "ideal":
        # fidelity_term_comparison_qasm(filename, list_data_name, generate_data)
        # comparison_dependency(filename, list_data_name, generate_data)
        comparison_dependency_technique(filename, list_data_name, "Optimality Analysis", "fig/comparison-ideal.pdf", list_color)
    
    elif generate_data == "technique_comp":
        comparison_dependency_technique(filename, list_data_name, "Technique Comparison", "fig/fig_11-comparison_technique.pdf", list_color)
    
    elif generate_data == "technique_comp_time":
        comparison_dependency_technique_time(filename, list_data_name, list_color)

    elif generate_data == "aod_comp":
        comparison_dependency_technique(filename, list_data_name, "AOD Number Comparison", "fig/fig_14-comparison_aod.pdf", list_color)
    
    elif generate_data == "aod_technique_comp":
        comparison_dependency_technique(filename, list_data_name, "AOD/technique Comparison", "fig/comparison-aod-placement.pdf", list_color)
    
    elif generate_data == "zone_comp":
        comparison_dependency_technique(filename, list_data_name, "Zone Comparison", "fig/sec_VII_H-comparison_zone.pdf", list_color)
    
    elif generate_data == "arch_comp":
        comparison_dependency_technique(filename, list_data_name, "Architecture Comparison", "fig/fig_8-comparison_arch.pdf", list_color)

    
    
# fidelity_breakdown_qasm_motivation("csv/fidelity_breakdown.csv", ["Enola"])