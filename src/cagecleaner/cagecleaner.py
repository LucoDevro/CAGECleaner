#!/bin/env python

"""
Usage: `cagecleaner <options>`

This python script cleans up redundant hits from the cblaster tool. It has primarly been developed and tested for usage with the NCBI nr database.

In its simplest use case, it takes the binary and summary output files as arguments, 
along with a percent identity cutoff to dereplicate the genomes using the skDER tool.
In addition, it recovers some of the gene cluster diversity lost by the dereplication by assessing gene cluster content and hit score outliers.

This tool will produce four final output files
    - cleaned_binary.csv: a file structured in the same way as the cblaster binary output, containing only the retained hits. 
    - clusters.txt: the corresponding cluster IDs from the cblaster summary file for each cleaned hit.
    - genome_cluster_sizes.txt: the number of genomes in a dereplication genome cluster, referred to by the dereplication representative genome.
    - genome_cluster_status.txt: a table with scaffoldz IDs, their representative genome assembly and their dereplication status.
    
There are four possible dereplication statuses:
    - 'dereplication_representative': this scaffold is part of the genome assembly that has been selected as the representative of a genome cluster.
    - 'readded_by_content': this scaffold has been kept as it contains a hit that is different in content from the one of the dereplication representative.
    - 'readded_by_score': this scaffold has been kept as it contains a hit that has an outlier cblaster score.
    - 'redundant': this scaffold has not been retained and is therefore removed from the final output.
"""

import pandas as pd
import sys
import subprocess
import re
import os
from more_itertools import batched
import gzip
import shutil
import argparse
from Bio import SeqIO
from scipy.stats import zscore
from random import choice

ACCESSIONS_SCRIPT = os.path.join(os.path.abspath(os.path.dirname(sys.argv[0])), 'get_accessions.sh')
DOWNLOAD_SCRIPT = os.path.join(os.path.abspath(os.path.dirname(sys.argv[0])), 'download_assemblies.sh')
DEREPLICATE_SCRIPT = os.path.join(os.path.abspath(os.path.dirname(sys.argv[0])), 'dereplicate_assemblies.sh')

GENOMES = "data/genomes"
SKDER_OUT = "data/skder_out"

def validate_input_files(path_to_binary: str, path_to_summary:str) -> bool:
    """
    This function takes the path to a binary and summary file as input and validates if the files are useable for downstream analysis.

    :param:path_to_binary: Path to the binary file.
    :param:path_to_summary: Path to summary file.
    :rtype bool: True if all checks pass.
    """
    
    try:
        
        print("--- STEP 0: Validating input files. ---")
        print("Validating binary file.")
        
        # First we check if the file exists:
        if not os.path.isfile(path_to_binary): 
            raise FileNotFoundError(f"'{path_to_binary}' does not exist. Please check if the binary file path is correct and try again.")
        
        # Now we check if the file has at least 6 columns:
        data = pd.read_table(path_to_binary, sep = "\\s{2,}", engine = 'python')
        assert len(data.columns) > 6, "The amount of columns does not correspond to a cblaster binary output file. Please check the file structure. At least 6 columns should be present."
        
        # Check if the required column names are present (Organism, Scaffold...):
        assert {"Organism", "Scaffold", "Start", "End", "Score"} < set(data.columns), ...
        "The column names do not correspond to a cblaster binary output file. Please check the file structure.\n The columns 'Organism', 'Scaffold', 'Start', 'End' and 'Score' should be present."
    
        # Check if there are at least two hits (3 because of header):
        assert len(data.index) > 3, "We are expecting at least two hits in the cblaster binary output file."
        
        print("CHECK") 
        
        print("Validating summary file.")
    
        # Again, check if the summary file exists:
        if not os.path.isfile(path_to_summary):
            raise FileNotFoundError(f"'{path_to_summary}' does not exist. Please check if the summary file path is correct and try again.")
        
        # The amount of appearances of the word "Cluster" in the summary file should correspond to the amount of lines
        # in the binary file:
        with open (path_to_summary, 'r') as file:
            assert len(re.findall("Cluster", file.read())) == len(data.index), "The numbers of identifiers in the binary and the summary file do no match." 
        
        print("CHECK")
        
        return True
    
    # In case an unknown error occurs, return that the validation did not succeed.
    except:
        return False 
   

def get_scaffolds(path_to_binary: str) -> list:
    """
    This function extracts the scaffold IDs from the cblaster binary output file.
    
    :param str path_to_binary: Path to the cblaster binary output file.
    :rtype list: A list containing Genbank and RefSeq Nucleotide IDs.
    """
    print("\n--- STEP 1: Extracting scaffold IDs. ---")

    # Read the file using a variable series of spaces as separator and extract the second column (containing the scaffold IDs)
    scaffolds = pd.read_table(path_to_binary, sep = "\\s{2,}", engine = 'python', usecols = [1])['Scaffold'].to_list()

    print(f"Extracted {len(scaffolds)} scaffold IDs")
    
    return scaffolds
        

def get_assemblies(scaffolds: list) -> list:
    """
    This function obtains the genome assembly ID for each scaffold ID obtained by get_scaffolds().
    It uses the NCBI Entrez-Direct utilities via a bash subprocess.

    :param list scaffolds: A list containing Genbank and RefSeq Nucleotide IDs.
    :rtype list: A list of Genbank and RefSeq Assembly IDs.
    """        

    print("\n--- STEP 2: Retrieving genome assembly IDs. ---")
    
    # save the scaffold list in a temporary file
    with open('scaffolds.txt', 'w') as handle:
        handle.writelines('\n'.join(scaffolds))
    
    # map to assembly IDs using E-utilities
    subprocess.run(['bash', ACCESSIONS_SCRIPT, 'scaffolds.txt'], check = True)
    
    # read the result file
    with open('assembly_accessions', 'r') as handle:
        assemblies = [l.rstrip() for l in handle.readlines()]
        
    # remove the temporary files
    os.remove('scaffolds.txt')
    os.remove('assembly_accessions')
    
    return assemblies

    
def download_genomes(assemblies: list, batch_size: int = 300) -> None:
    """
    This function downloads the full nucleotide fasta files of all found assemblies using the NCBI Datasets CLI via a bash subprocess.
    It automatically selects for the most recent accession version by omitting the version digit and relying on the NCBI Datasets CLI defaults 
    to download the most recent version.
    The assemblies are downloaded in batches of 300 by default and saved in the temporary folder data/genomes in the working directory.
    
    :param list assemblies: A list containing Genbank and RefSeq assembly IDs.
    :param int batch_size: The number of assemblies to download per batch. [default: 300]
    """
    
    print("\n--- STEP 3: Downloading genomes. ---")
    
    # Cut off the version digits
    versionless_assemblies = [acc.split('.')[0] for acc in assemblies]
    
    # Prepare the batches and save them in a temporary file
    with open('download_batches.txt', 'w') as file:
            download_batches = list(batched(versionless_assemblies, batch_size))
            for batch in download_batches:
                file.write(' '.join(batch) + '\n')

    # Run the bash script to download and cluster genomes:
    subprocess.run(["bash", DOWNLOAD_SCRIPT], check=True)
    
    # Remove temporary files
    os.remove('download_batches.txt')
    
    return None


def map_scaffolds_to_assemblies(scaffolds: list, assemblies: list) -> dict:
    """
    This function maps the scaffolds in the list of cblaster hits to the genome assembly they are part of. To get the exact scaffold IDs of each assembly,
    scaffolds are retrieved from the headers of the assembly fasta files. Prefixes are split off from both scaffold ID sets (the one from cblaster,
    and the one from the NCBI download) during mapping as scaffold IDs mapped by the E-utilities sometimes do not correspond with the ones
    in the downloaded fasta files.
        
    :param list scaffolds: A list containing Genbank and RefSeq Nucleotide IDs.
    :param assemblies: A list containing Genbank and RefSeq Assembly IDs.
    :rtype dict: A dictionary with scaffold Nucleotide IDs as keys and a containing assembly ID as value
    """
    
    print("\n--- STEP 4: Mapping local scaffold IDs to assembly IDs. ---")
    
    # Auxiliary function to split off prefixes from Nucleotide accession IDs
    def split_off_prefix(txt: str) -> str:
        if '_' not in txt:
            return txt
        else:
            return '_'.join(txt.split('_')[1:])
        
    # Auxiliary function to link the Nucleotide accession ID without prefix back to the original ID
    def map_back(no_prefix: str, scaffolds: list) -> str:
        return [s for s in scaffolds if no_prefix in s][0]
    
    scaffolds_set = set([split_off_prefix(s) for s in scaffolds]) # scaffolds from cblaster, deprefixed
    mappings = {}
    
    for assmbl in assemblies:
        
        # Find the path to the downloaded fasta file for this assembly ID
        try:
            assmbl_file = [a for a in os.listdir(GENOMES) if assmbl in a][0]
        except IndexError:
            print(f'No assembly file found for {assmbl}!')
            continue
        
        # All genomes were gzip-compressed
        with gzip.open(os.path.join(GENOMES, assmbl_file), "rt") as genome:
            try:
                # get all scaffold Nucleotide accession IDs in this assembly
                scaffolds_in_this_assembly = [record.id for record in SeqIO.parse(genome, 'fasta')]
                
                # split off any prefix
                scaffolds_in_this_assembly = [split_off_prefix(i) for i in scaffolds_in_this_assembly]
                
                # find the ones we have a cblaster hit for
                scaffolds_in_this_assembly = set(scaffolds_in_this_assembly)
                found_scaffolds_no_prefix = list(scaffolds_set.intersection(scaffolds_in_this_assembly))
                
                # map the deprefixed IDs back to the original ones
                found_scaffolds = [map_back(s, scaffolds) for s in found_scaffolds_no_prefix]
                
                # add a mapping item for all scaffolds with a cblaster hit
                for scaff in found_scaffolds:
                    mappings[scaff] = assmbl
                    
            except IndexError:
                print(f'No corresponding scaffold accession ID could be found for {assmbl}!')
                
    print(f"Found {len(mappings)} scaffold-assembly links")
    
    return mappings


def dereplicate_genomes(ani_threshold: float = 99.0, nb_cores: int = 1) -> None:
    """
    This function takes a list of assembly IDs and calls a helper bash script that dereplicates the genomes using skDER.
    The default ANI cutoff for dereplication is 99 %.
    
    A file called "dereplicated_assemblies.txt" is then generated by the helper script, in addition to skDER's output which can be found
    in the temporary folder data/skder_out in the working directory.'

    :param float ani_threshold: The percent identity cutoff for dereplicating genomes (see skDER docs) [default: 99.0]
    :param int nb_cores: The number of cores to be available for dereplication [default: 1]
    """
    print("\n--- STEP 5: Dereplicating genomes. ---")
    
    subprocess.run(['bash', DEREPLICATE_SCRIPT, str(ani_threshold), str(nb_cores)], check = True)
    
    return None


def parse_dereplication_clusters(scaffold_assembly_pairs: dict) -> pd.DataFrame:
    """
    This function parses the secondary clustering result file from skDER to characterise the size and the members of the genome clusters.
    It lists which genomes were selected as representative and to which genome cluster all assemblies belong.
    In addition, it produces a report file with the size of the genome clusters.
    
    :param dict scaffold_assembly_pairs: A dictionary mapping scaffold Nucleotide IDs to their overarching Assembly ID.
    :rtype Pandas dataframe: A dataframe with Nucleotide IDs in the index and columns 'representative' and 'dereplication_status',
                             which, resp., refer to the dereplication representative of that assembly,
                             and the dereplication status of that assembly ('dereplication_representative' or 'redundant').
    """
    print("\n--- STEP 6: Parsing dereplication genome clusters. ---")
    
    # Auxiliary function to extract assembly accession IDs from the file paths listed in the skDER output table
    def extract_assembly(path: str) -> str:
        pattern = re.compile("GC[A|F]_[0-9]{9}\\.[1-9]+")
        assembly = pattern.findall(path)[0]
        return assembly
    
    # Auxiliary function to rename the column names parsed from the skDER output
    def remap_type_label(label: str) -> str:
        mapping = {'representative_to_self': 'dereplication_representative',
                   'within_cutoffs_requested': 'redundant'}
        return mapping[label]
    
    # Auxiliary function to retrieve all scaffold Nucleotide IDs with a cblaster hit for a certain assembly ID
    def map_scaffolds(assembly: str, scaffold_assembly_pairs: dict) -> list:
        return [s for s,a in scaffold_assembly_pairs.items() if a == assembly]
    
    # Parse the skDER output table. Rename the columns and extract Assembly IDs on-the-fly.
    genome_clusters_df = pd.read_table(os.path.join(SKDER_OUT, "skDER_Clustering.txt"), 
                                       converters={'assembly': extract_assembly,
                                                   'representative': extract_assembly,
                                                   'dereplication_status': remap_type_label},
                                       names = ['assembly', 'representative', 'dereplication_status'],
                                       usecols = [0,1,4], header = 0, index_col = 'assembly'
                                       ).sort_values(by = ['representative','dereplication_status'])
    
    ## Replace Assembly accession IDs by Nucleotide IDs.
    ## If there are multiple Nucleotide IDs with a cblaster hit, the records will be replicated.
    
    # Deconstruct the initial dataframe from the parsing into a dictionary with index values as keys and data as values
    genome_clusters_records = genome_clusters_df.to_dict(orient = 'index')
    
    # Build a similar dictionary using Nucleotide accessions as keys, replicating data if needed
    genome_clusters = {}
    for assembly, data in genome_clusters_records.items():
        mapped_scaffolds = map_scaffolds(assembly, scaffold_assembly_pairs) # map assembly to a list of scaffold(s) with a cblaster hit
        for scaffold in mapped_scaffolds:
            genome_clusters[scaffold] = data
    
    # Construct a new dataframe from this Nucleotide-keyed dictionary
    genome_clusters = pd.DataFrame.from_dict(genome_clusters, orient = 'index', columns = ['representative', 'dereplication_status'])
    genome_clusters = genome_clusters.sort_values(by = ['representative','dereplication_status'])
    
    # Determine the size of each genome cluster and write report file
    clust_size = pd.DataFrame(genome_clusters.groupby(by = "representative")['representative'].count()).rename(columns = {'representative': 'size'})
    clust_size.to_csv('genome_cluster_sizes.txt', sep = "\t")
    print('Genome cluster sizes written in genome_cluster_sizes.txt')
    
    return genome_clusters


def recover_hits(path_to_binary: str, genome_clusters_mapping: pd.DataFrame, not_by_content: bool = False, not_by_score: bool = False, outlier_z: float = 2, min_score_diff: float = 0.1) -> pd.DataFrame:
    """
    This function recovers some variation in gene clusters that was lost due to dereplicating the hosting genomes. It offers two approaches to
    flag interesting gene clusters that will be kept in the output.
    
    1) Different gene cluster content
    A scaffold that is part of a dereplication cluster may have a different gene cluster content than its representative, i.e. a different number of
    identified homologs. The cblaster binary table also lists the number of homologs were found in a gene cluster for each query gene.
    If these numbers are different from the ones of the representative assembly, flag to keep this scaffold and its hit.
    
    2) Outlier cblaster score
    cblaster adds a 'Score' column in the binary output table that captures the numbers of homologs as well as aggregates the level of homology
    of the entire cluster. If this score is significantly different from the other scores, this may indicate multiple gene cluster lineages within
    this genome cluster, or a remarkably fast evolving gene cluster. In any case, it is interesting to retain this hit.
    Signficance is currently determined using z-scores for each cluster content group.
    
    :param str path_to_binary: path to the cblaster binary result table
    :param pandas Dataframe genome_clusters_mapping: dataframe returned by the parse_dereplication_clusters() function, containing the dereplication
                                                     status and representative of each assembly
    :param bool not_by_content: flag to disable recovering gene clusters by gene cluster content, also disables recovery by outlier score [default: False]
    :param bool not_by_score: flag to disable recovering gene clusters by outlier cblaster score [default: False]
    :param float outlier_z: minimum absolute value of the z-score to consider a hit as an outlier
    :param float min_score_diff: minimum difference in cblaster score between a hit and the mode score when determining outlier scores.
                                 Outliers with a score difference below this value are discarded. [default: 0.1]
    :rtype Pandas dataframe: updated dereplication status table, now also containing flags to keep scaffolds and their gene clusters in the final output
    """
    print("\n--- STEP 7: Recovering gene cluster diversity. ---")
    
    # Make a copy of the original genome cluster table
    updated_mapping = genome_clusters_mapping.copy()
    
    # Skip this if not recovering by gene cluster content
    if not(not_by_content):
        
        # Read the relevant columns from the cblaster binary table
        with open(path_to_binary, 'r'):
            hits = pd.read_table(path_to_binary, sep = "\\s{2,}", engine = 'python', 
                                 usecols = lambda x: x not in ['Organism', 'Start', 'End'],
                                 index_col = "Scaffold")
        
        ## Hits are recovered within dereplication clusters so we will check the hits of each dereplication cluster
        # Get the IDs of all assemblies grouped by their dereplication representative
        grouped_mapping = genome_clusters_mapping.reset_index(names = 'scaffold').groupby('representative').agg(list)
        genome_groups = dict(zip(grouped_mapping.index, grouped_mapping['scaffold']))
        
        for representative, group in genome_groups.items():

            # Get the cblaster hit data for scaffolds of this dereplication cluster
            hits_this_group = hits.loc[group]
            
            # Split the dereplication grouping further into gene cluster subgroups as reflected by the number of homologs of each query gene
            hits_this_group_by_content_group = [l.to_list() for l in hits_this_group.groupby(
                                                    list(set(hits_this_group.columns).difference({'Score'})) # to get all query gene columns
                                                    ).groups.values()]
            
            # Loop over all structural subgroups
            for content_group in hits_this_group_by_content_group:
                scores_this_content_group = hits_this_group.loc[content_group, 'Score'] # cblaster scores
                mode_score_this_content_group = float(scores_this_content_group.mode().iloc[0]) # modal cblaster score
                zscores_this_content_group = scores_this_content_group.transform(zscore) # zscores
                
                # If the result of the z-score calculation yields all NaNs, all cblaster scores were identical, implying there is no score outlier.
                # In that case, we can continue flagging different gene cluster contents, if any
                # If the user is not interested in flagging score outlier hits, we end up in this case anyway.
                if not_by_score or zscores_this_content_group.isna().all():
                    
                    # If the dereplication representative is one of the assemblies that has a scaffold in this structural subgroup, 
                    # then we already have a hit from this subgroup, so we can skip this one.
                    dereplication_status_this_content_group = [genome_clusters_mapping.loc[m, 'dereplication_status'] for m in content_group]
                    if "dereplication_representative" in dereplication_status_this_content_group:
                        continue
                    
                    # If the dereplication representative is not in this structural subgroup, randomly pick a member as a representative hit to keep
                    else:
                        content_group_representative = choice(content_group)
                        updated_mapping.at[content_group_representative, 'dereplication_status'] = 'readded_by_cluster_content'
                
                # The case there were different cblaster scores and the user is interested in score outliers
                else:
                    
                    # Select hits that have an absolute z-score above the threshold,
                    # and of which the cblaster score is sufficiently different from the modal score,
                    # to avoid 'false' outliers that are just a little bit more different than most hits
                    outliers_this_content_group = zscores_this_content_group.loc[
                        (zscores_this_content_group.abs() >= outlier_z) &
                        ((scores_this_content_group - mode_score_this_content_group).abs() >= min_score_diff)].index
                    
                    # All score outliers will be added
                    for outlier in outliers_this_content_group.to_list():
                        # If the overarching assembly of this outlier is the dereplication representative, skip flagging it
                        dereplication_status_outlier = genome_clusters_mapping.loc[outlier, 'dereplication_status']
                        if dereplication_status_outlier == "dereplication_representative":
                            continue
                        else:
                            updated_mapping.at[outlier, 'dereplication_status'] = 'readded_by_outlier_score'
                            
                    # We still have to flag a non-outlier representative of this structural subgroup
                    non_outliers_this_content_group = zscores_this_content_group.drop(index = outliers_this_content_group).index.to_list()
                    
                    # If this subgroup contains a scaffold of the dereplication representative, skip this subgroup
                    dereplication_status_non_outliers_this_content_group = [genome_clusters_mapping.loc[m, 'dereplication_status'] 
                                                                              for m in non_outliers_this_content_group]
                    if "dereplication_representative" in dereplication_status_non_outliers_this_content_group:
                        continue
                    
                    # If not, pick a random representative from the non-outlier hits
                    else:
                        content_group_representative = choice(non_outliers_this_content_group)
                        updated_mapping.at[content_group_representative, 'dereplication_status'] = 'readded_by_cluster_content'
    
    # There is nothing to recover in this case
    else:
        print('All revisiting options have been disabled. Not updating the genome grouping labels.')
    
    # Tidy up the updated mapping and write report file
    updated_mapping = updated_mapping.sort_values(by = ['representative', 'dereplication_status'])
    updated_mapping.reset_index(names = 'scaffold').to_csv('genome_cluster_status.txt', sep = "\t", index = False)
    
    return updated_mapping

                            
def get_dereplicated_scaffolds(genome_clusters: pd.DataFrame) -> list:
    """
    This function retrieves the final retained scaffold IDs, after dereplication and hit recovery.

    :param Pandas dataframe; A dataframe containing the final status of each scaffold and its dereplication representative
    :rtype list: A list containing the retained scaffolds
    """  
    print("\n--- STEP 8: Gathering retained scaffold IDs. ---")
    
    dereplicated_scaffolds = genome_clusters[genome_clusters['dereplication_status'] != 'redundant'].index.to_list()

    print(f"Got {len(dereplicated_scaffolds)} representative scaffold IDs")
    
    return dereplicated_scaffolds


def write_output(dereplicated_scaffolds:list, path_to_summary:str, path_to_binary:str) -> None:
    """
    This function takes a list of retained scaffold IDs and the cblaster summary and binary output files.
    It writes the corresponding Cluster IDs and cblaster hit entries to a file.

    :param list dereplicated_scaffolds: A list containing the retained scaffold IDs.
    :param str path_to_summary: Path to summary file.
    :param str path_to_binary: Path to binary file.
    """
    print("\n--- STEP 9: Generating output files. ---")
    
    ## First we do the binary file:

    # Create intermediary list to store the cleaned hits:
    cleaned_hits = []

    with open(path_to_binary, 'r',) as file:
        file_content = file.read()
        # We also capture the header to write to our cleaned file later:
        header = file_content.split("\n")[0] + "\n" 

        # Loop over the cleaned scaffold IDs and match them in the binary file using regex.
        for scaffold in dereplicated_scaffolds:
            pattern = f".*{scaffold}.*"
            # Append to the list of cleaned hits:
            cleaned_hits.append(re.search(pattern, file_content).group(0))
        
    with open('cleaned_binary.txt', 'w') as file:
        file.write(header)
        for cleaned_hit in cleaned_hits:
            file.write(f"{cleaned_hit}\n")    

    ## Secondly, the cluster IDs. Same principle but then with the summary file:

    # Create an intermediary list to store the cluster IDs:
    clusters = []
    
    with open(path_to_summary, 'r') as file:
        file_content = file.read()
        
        # Loop over each scaffold ID
        for scaffold in dereplicated_scaffolds:
            pattern = f"({scaffold}\n[-]*\n)(Cluster \\d*)"
            clusters.append(re.search(pattern, file_content).group(2))
    
    with open('clusters.txt', 'w') as file:
        for cluster in clusters:
            file.write(f"{cluster}\n")
            
    return None
    
    
def parse_arguments():
    """
    Argument parsing function
    """
    
    # Auxiliary function to check the value range of the ANI threshold
    def check_percentage(value):
        if 0 <= float(value) <= 100:
            return value
        else:
            raise argparse.ArgumentTypeError("%s should be a percentage value between 0 and 100" % value)
    
    parser = argparse.ArgumentParser(prog = 'ccleaner', description = "Tool to reduce redundancy in cblaster hits")
    parser.add_argument('-o', '--output', dest = "work_dir", default = '.')
    parser.add_argument('-b', '--binary', dest = "binary")
    parser.add_argument('-s', '--summary', dest = "summary")
    parser.add_argument('-c', '--cores', dest = 'cores', default = 1)
    parser.add_argument('-a', dest = 'ani', default = 99.0, type = check_percentage)
    parser.add_argument('--no-content-revisit', dest = 'no_revisit_by_content', default = False, action = "store_true")
    parser.add_argument('--no-score-revisit', dest = 'no_revisit_by_score', default = False, action = "store_true")
    parser.add_argument('--min-z-score', dest = 'outlier_z', default = 2.0)
    parser.add_argument('--min-score-diff', dest = 'min_score_diff', default = 0.1)
    parser.add_argument('--download-batch', dest = 'download_batch', default = 300)
    parser.add_argument('--validate-files', dest = "validate_inputs", default = False, action = "store_true")
    parser.add_argument('--keep-downloads', dest = "keep_downloads", default = False, action = "store_true")
    parser.add_argument('--keep-dereplication', dest = "keep_dereplication", default = False, action = "store_true")
    parser.add_argument('--keep-intermediate', dest = "keep_intermediate", default = False, action = "store_true")
    args = parser.parse_args()
    
    return args

def main():
    """
    Main function
    """
    args = parse_arguments()
    
    work_dir = args.work_dir.rstrip('/')
    path_to_binary = os.path.join(os.getcwd(), args.binary)
    path_to_summary = os.path.join(os.getcwd(), args.summary)
    nb_cores = int(args.cores)
    ani_threshold = float(args.ani)
    no_revisit_by_content = args.no_revisit_by_content
    no_revisit_by_score = args.no_revisit_by_score
    outlier_z = float(args.outlier_z)
    min_score_diff = float(args.min_score_diff)
    download_batch_size = int(args.download_batch)
    validate_inputs = args.validate_inputs
    keep_downloads = args.keep_downloads
    keep_intermediate = args.keep_intermediate
    keep_dereplication = args.keep_dereplication
    
    os.makedirs(work_dir, exist_ok = True)
    os.chdir(work_dir)
     
    # Validate the input files. If a covered assertation fails, it will fail inside the function. An unknown error is captured by this
    # main assert statement
    if validate_inputs:
        assert validate_input_files(path_to_binary, path_to_summary), ...
        "Something was wrong with the input files! Please check them and try again."
    
    # extract scaffold IDs from the cblaster output
    scaffolds = get_scaffolds(path_to_binary)
    # link to assembly IDs via the NCBI E-utilities
    assemblies = get_assemblies(scaffolds)
    # download assemblies using the NCBI Datasets CLI
    download_genomes(assemblies, download_batch_size)
    # map the downloaded scaffold IDs to assembly IDs
    scaffold_assembly_pairs = map_scaffolds_to_assemblies(scaffolds, assemblies)
    # dereplicate the genomes using skDER
    dereplicate_genomes(ani_threshold, nb_cores)
    # parse the secondary clustering from the skDER output and construct a dereplication status table
    genome_cluster_composition = parse_dereplication_clusters(scaffold_assembly_pairs)
    # recover gene cluster hits and update 
    updated_status = recover_hits(path_to_binary, genome_cluster_composition, no_revisit_by_content, no_revisit_by_score, outlier_z, min_score_diff)
    # retrieve the finally retained scaffold IDs from the updated status table
    dereplicated_scaffolds = get_dereplicated_scaffolds(updated_status)
    # generate final output files
    write_output(dereplicated_scaffolds, path_to_summary, path_to_binary)
    
    # Finish by removing intermediate results if requested
    if not(keep_intermediate):
        if not(keep_downloads) and not(keep_dereplication):
            shutil.rmtree('data')
        else:
            if not(keep_downloads):
                shutil.rmtree(GENOMES)
            if not(keep_dereplication):
                shutil.rmtree(SKDER_OUT)
    
    print(f"All done! Results are written to {work_dir}")
            
if __name__ == "__main__":
    main()
