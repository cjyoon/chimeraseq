import sys
import os
import subprocess
import shlex
import re
import argparse
import json
import multiprocessing as mp
import pysam
import gzip

def argument_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--father', required=True, help="Father's bam file")
    parser.add_argument('-m', '--mother', required=True, help="Mother's bam file")
    parser.add_argument('-c', '--child', required=True, help="Child's bam file")
    parser.add_argument('-r', '--reference', required=True, help="Reference fasta file")
    parser.add_argument('-s', '--snp', required=False, default=None, help="Optional list of SNP sites as a BED file")
    parser.add_argument('-t', '--thread', required=False, default=1, type=int, help="Multithread to utilize")
    parser.add_argument('-o', '--output_dir', required=False, default=os.getcwd(), help='Output directory')
    parser.add_argument('-p', '--prefix', required=False, default=None, help="prefix for the output file. If not specified, will use the SM tag from the child's bam")
    args = vars(parser.parse_args())

    if args['prefix'] == None:
        args['prefix'] = sampleNameBam(args['child'])
    return args['father'], args['mother'], args['child'], args['reference'], args['snp'], args['thread'], args['output_dir'], args['prefix']


def sampleNameBam(bamFile):
    """get @RG SM: information as sample name from BAM header"""
    bam = pysam.AlignmentFile(bamFile)
    name = bam.header['RG'][0]['SM']
    return name


def identify_autosomal_chromosomes(fasta_file):
    """given fasta file, identify the chromosomes that are autosomal"""
    fai_file = fasta_file +  '.fai'
    if(os.path.exists(fai_file)):
        with open(fai_file, 'r') as f:
            for line in f:
                chrom, length, *args = line.split('\t')
                if re.search(r'^chr[0-9]+$|^[0-9]+$', chrom):
                    yield (chrom, float(length))
    else:
        print(f'There is no index file for {fasta_file}. Exiting...')
        sys.exit(1)


def split_regions(fasta_file, segment_length):
    """splits chromosome into segment lengths"""
    chr_regions = []
    for chrom, chr_length in identify_autosomal_chromosomes(fasta_file):
        for i in range(0, int(chr_length/segment_length)):
            start = i*segment_length
            end = (i+1)*segment_length

            if(chr_length-end <= segment_length):
                end = chr_length
            chr_regions.append(f'{chrom}:{start:.0f}-{end:.0f}')
    return chr_regions


def check_gzip_file(file_path):
    """checks if the file is binary or not"""
    cmd = f'file {file_path}'
    execute = subprocess.check_output(shlex.split(cmd)).decode()
    if re.search(r'gzip compressed|gzip compatible', execute):
        return True
    else:
        return False


def check_region_and_snp_bed(region, snp_bed):
    """if a region does not contain anything on the bed varscan output has no variant, it will cause error downstream when merging"""
    region_chromosome, start_end = region.split(':')
    region_start, region_end = start_end.split('-')
    region_start = float(region_start)
    region_end = float(region_end)
    variant_count = 0 

    is_snp_bed_gzip = check_gzip_file(snp_bed)

    # determine gzip compression of snp bed file and use appropriate file handler
    if is_snp_bed_gzip==True:
        f = gzip.open(snp_bed, 'rt')
    else:
        f= open(snp_bed, 'r')


    for line in f:
        chrom, start, end, *args = line.strip().split('\t')
        if chrom == region_chromosome:
            if float(start) > region_start and float(start) < region_end:
                variant_count += 1
            
            if variant_count > 0:
                f.close()
                return True
    # if no variant within the region is found then return False
    f.close()
    return False


def filter_regions_with_snv(region_list, snp_bed):
    """using check_region_and_snp_bed remove regions with no overlaping snp in the region"""
    keep_list = []
    for region in region_list:
        if check_region_and_snp_bed(region, snp_bed):
            keep_list.append(region)
        else:
            print(f'{region} filtered out since it does not have any of the SNPs in the BED file')

    return keep_list

def mpileup(father_bam, mother_bam, child_bam, region, output_dir, snp_bed):
    """Run mpileup in a defined region of interest"""
    
    child_id = sampleNameBam(child_bam)
    region_string = re.sub(r':|-', '_', region)
    # print(region_string)

    # if SNP-BED file is provided 
    if snp_bed != None:
        snp_bed_string = f' -l {snp_bed} '
    else:
        snp_bed_string = ''

    # output_file = os.path.join(output_dir, f'{child_id}_{region_string}.mpileup')
    output_file_compressed = os.path.join(output_dir, f'{child_id}_{region_string}.mpileup.gz')
    print(output_file_compressed)

    if not os.path.isfile(output_file_compressed):
        cmd = f'{SAMTOOLS} mpileup -B -Q 20 -q 20 {snp_bed_string}-r {region} -f {REFERENCE} {father_bam} {mother_bam} {child_bam} | gzip -f > {output_file_compressed}'
        os.system(cmd)
    # gzip_cmd = f'{GZIP} -f ' 
    # gzip_execute = subprocess.Popen(shlex.split(gzip_cmd), stdin=subprocess.PIPE, shell=True, stderr=subprocess.DEVNULL)
    
    
    # mpileup_cmd = f'{SAMTOOLS} mpileup -B -Q 20 -q 20 {snp_bed_string}-r {region} -f {REFERENCE} {father_bam} {mother_bam} {child_bam} '
    # mpileup_execute = subprocess.Popen(shlex.split(mpileup_cmd), stdout=gzip_execute.stdin, stderr=subprocess.DEVNULL, shell=True)
    
    # mpileup_execute.wait()
    # gzip_execute.stdin.close()
    # gzip_execute.wait()

    return  output_file_compressed



def vaf(alt_count, depth):
    if depth > 0:
        return float(alt_count/depth)
    else:
        return 0


def count_int(count):
    """handle '.' in alt/ref counts"""
    if count == None or count < 0:
        return 0
    else:
        return count


def natural_sort(l):
    def convert(text): return int(text) if text.isdigit() else text.lower()

    def alphanum_key(key): return [convert(c)
                                   for c in re.split('([0-9]+)', key)]
    return sorted(l, key=alphanum_key)


def parse_mpileup(mpileup_line):
    """Parse mpileup result into counts string"""
    split_mpileup = mpileup_line.split('\t'); #print(split_mpileup)
    chrom = split_mpileup[0]
    pos = float(split_mpileup[1])
    ref = split_mpileup[2]

    totalDepth = 0 
    mismatches = 0
    totalCharacters = 0
    trio_alt_counts = dict({'father': None, 'mother': None, 'child': None})
    trio_depth_counts = dict({'father': None, 'mother': None, 'child': None})
    for individual, i in zip(['father', 'mother', 'child'], range(1, 4)):
        # initialize mismatch dict for each bam
        mismatch_dict = dict({'A': 0, 'C': 0, 'G': 0, 'T': 0, 'ins': 0, 'del': 0, 'depth': 0})

        bases_index = 3*i + 1
        depths_index = 3*i
        depths = int(split_mpileup[depths_index])
        totalDepth += depths
        mpiledup = split_mpileup[bases_index].upper()
        insertions = re.findall(r'\+[0-9]+[ACGTNacgtn]+', mpiledup)
        deletions = re.findall(r'-[0-9]+[ACGTNacgtn]+', mpiledup)

        mismatch_dict['ins'] = len(insertions)
        mismatch_dict['del'] = len(deletions)

        mpileupsnv = re.sub(r'\+[0-9]+[ACGTNacgtn]+|-[0-9]+[ACGTNacgtn]+', '', mpiledup)


        mismatch_dict['A'] = mpileupsnv.count('A')
        mismatch_dict['T'] = mpileupsnv.count('T')
        mismatch_dict['G'] = mpileupsnv.count('G')
        mismatch_dict['C'] = mpileupsnv.count('C')
        trio_depth_counts.update({individual: depths})
        trio_alt_counts.update({individual: mismatch_dict})
        
        

    # now parse the dictionaries into a output string
    father_depth = trio_depth_counts['father']
    mother_depth = trio_depth_counts['mother']
    father_alt_base = [k for k, v in trio_alt_counts['father'].items() if v > 0]

    mother_alt_base = [k for k, v in trio_alt_counts['mother'].items() if v > 0]

    n_alt_base_father = len(father_alt_base)
    n_alt_base_mother = len(mother_alt_base)
    if (n_alt_base_father==1 and n_alt_base_mother==0):
        alt_base = ''.join(father_alt_base)
        alt_parent = 'F'
        alt_father = trio_alt_counts['father'][alt_base]
        alt_mother = trio_alt_counts['mother'][alt_base]
        child_alt = trio_alt_counts['child'][alt_base]
        father_vaf = vaf(alt_father, father_depth)
        mother_vaf = vaf(alt_mother, mother_depth)
    elif (n_alt_base_father==0 and n_alt_base_mother==1):
        alt_base = ''.join(mother_alt_base)
        alt_parent = 'M'
        alt_father = trio_alt_counts['father'][alt_base]
        alt_mother = trio_alt_counts['mother'][alt_base]
        child_alt = trio_alt_counts['child'][alt_base]
        father_vaf = vaf(alt_father, father_depth)
        mother_vaf = vaf(alt_mother, mother_depth)

    elif (n_alt_base_father==0 and n_alt_base_mother==0):
        alt_base='N' # parents homoref
        alt_parent='NA'
        alt_father = 0
        alt_mother=0
        # if parents are homoref/homoref, any non ref bases are errors
        child_alt = sum(trio_alt_counts['child'].values())
        father_vaf = vaf(alt_father, father_depth)
        mother_vaf = vaf(alt_mother, mother_depth)

    else:
        father_vaf = -1 # arbitrary to filter out
        mother_vaf = -1 # arbitrary to filter out

        pass
    snvcount = ''
    if (father_vaf > 0.4 and father_vaf < 0.6 and mother_vaf ==0) or (mother_vaf > 0.4 and mother_vaf < 0.6 and father_vaf ==0):
        if father_depth > 10 and mother_depth > 10:
            child_depth = trio_depth_counts['child']
            child_vaf = vaf(child_alt, child_depth)
            snvcount = f'{chrom}\t{pos:.0f}\t{ref}\t{alt_base}\t{child_alt}\t{child_depth}\t{child_vaf}\t{alt_parent}\tNA\t{father_vaf}\t{mother_vaf}\n'
    elif (father_vaf==0 and mother_vaf ==1) or (father_vaf==1 and mother_vaf==0):
        if father_depth > 10 and mother_depth > 10:
            child_depth = trio_depth_counts['child']
            child_vaf = vaf(child_alt, child_depth)
            snvcount = f'{chrom}\t{pos:.0f}\t{ref}\t{alt_base}\t{child_alt}\t{child_depth}\t{child_vaf}\tNA\t{alt_parent}\t{father_vaf}\t{mother_vaf}\n'
    elif (father_vaf==0 and mother_vaf==0):
        if father_depth > 10 and mother_depth > 10:
            child_depth = trio_depth_counts['child']
            child_vaf = vaf(child_alt, child_depth)
            snvcount = f'{chrom}\t{pos:.0f}\t{ref}\t{alt_base}\t{child_alt}\t{child_depth}\t{child_vaf}\tNA\tNA\t{father_vaf}\t{mother_vaf}\n'
    return snvcount


def get_parent_het_homref_child_count(mpileup_file):
    """parse mpileup results into a table"""
    output_counts_region = mpileup_file +'.counts'
    with open(output_counts_region, 'w') as g:
        g.write('chrom\tpos\trefbase\taltbase\talt\tdepth\tvaf\thetero_parent\thomoalt_parent\tfather_vaf\tmother_vaf\n')
        with gzip.open(mpileup_file, 'rt') as f:
            for line in f:
                g.write(parse_mpileup(line))

        f.close()
    g.close()

    return output_counts_region
        

def run_mle_rscript(count_table, output_dir, run_mode):
    """run mode can be either optim for quickly running mle, and plot if you want to get mle plot for all possible estimation"""

    cmd = f'{RSCRIPT} {MLE_RSCRIPT} -i {count_table} -o {output_dir} -r {run_mode}'
    print(cmd)
    execute = subprocess.Popen(shlex.split(cmd))
    execute.wait()

    return 0 


def get_paths(path_config):
    """configures the paths to SAMTOOLS AND VARSCAN"""
    with open(path_config) as f:
        path = json.load(f)
    return path['SAMTOOLS'],  path['RSCRIPT'], path['GZIP']


def main():
    global SAMTOOLS, REFERENCE, RSCRIPT, MLE_RSCRIPT, GZIP

    father_bam, mother_bam, child_bam, REFERENCE, snp_bed, thread, output_dir, prefix = argument_parser()
    output_dir = os.path.abspath(output_dir)

    # configure paths to executables 
    script_dir = os.path.dirname(os.path.realpath(__file__)) 
    path_config = os.path.join(script_dir, 'path_config.json')

    SAMTOOLS, RSCRIPT, GZIP = get_paths(path_config)

    # path to the MLE Rscript 
    MLE_RSCRIPT = os.path.join(script_dir, 'chimera_likelihood.R')


    # split up regions
    segment_length = 50000000
    region_splits = split_regions(REFERENCE, segment_length)

    # if SNP bed is supplied, filter out those regions where no variant is found. 
    if snp_bed != None:
        region_splits= filter_regions_with_snv(region_splits, snp_bed)
    else:
        pass

    # run mpileup parallelly
    tmp_region_dir = os.path.join(output_dir, prefix + '_tmp')
    os.system(f'mkdir -p {tmp_region_dir}')
    arg_list = []
    for region in region_splits:
        arg_list.append((father_bam, mother_bam, child_bam, region, tmp_region_dir, snp_bed))

    # run with multithreading
    print('variant calling')
    with mp.Pool(thread) as pool:
        mpileup_files = pool.starmap(mpileup, arg_list)

    # go through mpileup files to parse information
    print('parsing mpileup')
    with mp.Pool(thread) as pool:
        counts_split_files = pool.map(get_parent_het_homref_child_count, mpileup_files)


    print(counts_split_files)


    # combine counts files
    combined_counts = os.path.join(output_dir, f'{prefix}.counts')
    count = 0
    with open(combined_counts, 'w') as f:
        for count_file in counts_split_files:
            with open(count_file, 'r') as g:
                if count != 0:
                    next(g) # write header only in the first file, otherwise skip first row
                count += 1
                for line in g:
                    f.write(line)


    # # maximum likelihood estimate
    print('running MLE')
    run_mle_rscript(combined_counts, output_dir, run_mode='optim')


if __name__=='__main__':
    main()