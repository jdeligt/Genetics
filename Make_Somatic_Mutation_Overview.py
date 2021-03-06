#!/usr/bin/python
import os
import re
import vcf
import glob
import numpy as np

import json
import requests
import pickle

#GENE FORMAT
##chr    start    stop    name
#3    178866311    178952497    PIK3CA

from optparse import OptionParser
# -------------------------------------------------
parser = OptionParser()
parser.add_option("--vcfdir",   dest="vcfdir",     help="Path to directory containing VCF files",  default=False)
parser.add_option("--outdir",   dest="outdir",     help="Path to directory to write output to",    default="./DriverProfile/")
parser.add_option("--genelist", dest="genelist",   help="File containing Genes to test/plot)",     default=False)
parser.add_option("--canon",    dest="canonical",  help="Only report Canonical effects",           default=False)

parser.add_option("--bgzip",    dest="bgzip",      help="Path to bgzip binary",                    default="bgzip")
parser.add_option("--tabix",    dest="tabix",      help="Path to tabix binary",                    default="tabix")

parser.add_option("--t",        dest="nrcpus",     help="Number of CPUs to use per sample",        default=2)

parser.add_option("--dp",       dest="mindepth",   help="Minimum read depth to consider reliable", default=10)
parser.add_option("--af",       dest="minvaf",     help="Minimum variant allele fraction",         default=0.25)
parser.add_option("--pf",       dest="popfreq",    help="Maximum popultaion frequency",            default=0.05)
parser.add_option("--cf",       dest="cohfreq",    help="Maximum cohort frequency",                default=0.10)
parser.add_option("--me",       dest="mineff",     help="Minimum variant effect score",            default=1.50)

parser.add_option("--debug",    dest="debug",      help="Flag for debug logging",                  default=False)
parser.add_option("--format",   dest="format",     help="VCF output format [GATK/FREEB/..]",       default="GATK")
(options, args) = parser.parse_args()
# -------------------------------------------------

# -------------------------------------------------
vocabulary = {
    "None":-1, "clean":0,
    "sequence_feature":0, "intron_variant":0,
    "3_prime_UTR_variant":0, "5_prime_UTR_variant":0, "non_coding_exon_variant":0,
    "TF_binding_site_variant":0.5, "splice_region_variant":0.5,
    "synonymous_variant":1.0,
    "missense_variant":1.5,
    "splice_donor_variant":2, "splice_acceptor_variant":2,
    "inframe_deletion":2.1, "inframe_insertion":2.1,
    "disruptive_inframe_deletion":2.5, "disruptive_inframe_insertion":2.5,
    "5_prime_UTR_premature_start_codon_gain_variant":3,
    "stop_gained":4, "nonsense_mediated_decay":4, "frameshift_variant":4
}

# Mapping of SNEPeff effects to 'MAF' names for variation effects, enables later use in MAF tools
# https://wiki.nci.nih.gov/display/TCGA/Mutation+Annotation+Format+%28MAF%29+Specification+-+v1.0
# https://bioconductor.org/packages/3.7/bioc/vignettes/maftools/inst/doc/maftools.html
mapping = {
    "synonymous_variant":"Silent", "missense_variant":"Missense_Mutation", "disruptive_inframe_deletion":"Frame_Shift_Del", "disruptive_inframe_insertion":"Frame_Shift_Ins",
    "5_prime_UTR_premature_start_codon_gain_variant":"Nonsense_Mutation", "stop_gained":"Nonsense_Mutation", "nonsense_mediated_decay":"Nonsense_Mutation", "frameshift_variant":"Frame_Shift_???"
}
# Data fields needed to make lollipop plots
lollipop = ["Hugo_Symbol","Sample_ID","Protein_Change","Mutation_Type","Chromosome","Start_Position","End_Position","Reference_Allele","Variant_Allele","VAF"]

# Known fields with information on population frequency
FREQ_FIELDS = ["dbNSFP_ExAC_AF", "dbNSFP_ExAC_Adj_AF", "GoNLv5_Freq", "GoNLv5_AF"]

CANONICAL_TRANSCRIPTS = {}


# -------------------------------------------------
# DETERMINE which effects to report based on 'abribitrary' variant impact score
toselect = [k for k,v in vocabulary.items() if v >= float(options.mineff)]
# -------------------------------------------------


# -------------------------------------------------
debug = options.debug
DEPTH_KEY=""
VAF_KEY=""
# -------------------------------------------------
def check_arguments():
    global DEPTH_KEY
    global VAF_KEY

    if not os.path.exists(options.vcfdir):
        print("Invalid VCF folder %s"%(options.vcfdir))
        return False

    if not os.path.exists(options.outdir):
        print("Creating output folder %s"%(options.outdir))
        try:
            os.mkdir(options.outdir)
        except OSError:
            print("Invalid / unable to create, output folder %s"%(options.outdir))
            return(False)

    if options.format == "GATK":
        DEPTH_KEY="AD"
        VAF_KEY="AD"

    if options.format == "FREEB":
        DEPTH_KEY="DP"
        VAF_KEY="DPR"


    print("Running with the following settings:")
    print("------------------------------------")
    print(options)
    print("DEPTH FIELD:"+DEPTH_KEY)
    print("ALLELE FIELD:"+VAF_KEY)
    print("------------------------------------")
    return(True)

# -------------------------------------------------

# Extract population frequency from VCF record
# Annoation assumed to be in SNPeff formatting
def find_popfreq(vcf_record):
    popfreq=[0.0]
    for field in FREQ_FIELDS:
        if field in vcf_record.INFO:
            #if debug: print(vcf_record.INFO[field])
            for x in vcf_record.INFO[field]:
                if x is None:
                    popfreq.append(0.0)
                else:
                    popfreq.append(float(x))
    return(popfreq)

# Determine the most damaging effect of the variant
def find_effects(vcf_record, sample_gt):
    maxeffect="None"
    if debug: print(vcf_record.INFO)

    if "ANN" not in vcf_record.INFO:
        return maxeffect

    # TRAVERSE ALL ANNOTATIONS
    for pred in vcf_record.INFO["ANN"]:
        # SPLIT THE SEPERATE FIELDS WITHIN THE ANNOTATION
        items = pred.split("|")
        #if debug: print("~~~\t"+items[3]+"\t"+items[4]+"\n"+"|".join(items))

        # Skip if annotation ALT allele does not match sample ALT allele
        if str(items[0]) != str(sample_gt):
            if debug: print("SKIPPING DUE TO MISMATCHING GENOTYPE\t|{}|\t|{}|".format(items[0], sample_gt))
            continue

        # IF Canonical only mode, skip all other transcripts
        if options.canonical:
            gene = items[4]
            if len(gene) <= 1:
                continue
            if gene not in CANONICAL_TRANSCRIPTS:
                 CANONICAL_TRANSCRIPTS[gene] = get_canonical(gene)
            if debug: print("~~~\t"+items[6]+" "+gene+" "+CANONICAL_TRANSCRIPTS[gene])
            if items[6] != CANONICAL_TRANSCRIPTS[gene]:
                continue

        allele = items[0]
        effects = items[1].split("&")
        for effect in effects:
            if debug: print(effect)
            if effect not in vocabulary:
                # A NEW MUTATION EFFECT WAS FOUND
                if debug:
                    print("NEW Mutation effect identified:")
                    print(pred)
                    print(effect)

            else:
                # STORE THE MOST DELETERIOUS EFFECT
                if vocabulary[effect] > vocabulary[maxeffect]:
                    maxeffect = effect
    if debug: print(maxeffect)
    return(maxeffect)

# ETRACT THE MOST DELETERIOUS MUTATIONS IN A GENE
def select_maximum_effect(effects):
    effectvalues = [vocabulary[eff] for eff in effects]
    if debug: print(effectvalues)
    indices = np.argmax(effectvalues)
    return(indices)

# CHECK AND GENERATE GZ AND TBI
def zip_and_index(vcffile):
    if not os.path.exists(vcffile+".gz"):
        os.system(options.bgzip+" -c "+vcffile+" > "+vcffile+".gz")
    if not os.path.exists(vcffile+".gz"+".tbi"):
        os.system(options.tabix+" "+vcffile+".gz")

# -------------------------------------------------
# GENE FORMAT
# Gene name + location + variants or not
# VARIANT FORMAT
# Variant + DEPTH + POP FREQ + MLEAF + EFFECT

def check_ad(sample_vcf):
    try:
        ad_item = sample_vcf[DEPTH_KEY]
    except AttributeError as e:
        return(False)
    if sample_vcf[DEPTH_KEY] is None:
        return(False)
    return(True)

#sample_vcf == vcf_record.genotype(sample)
def check_depth(sample_vcf):
    #single depth field
    if isinstance(sample_vcf[DEPTH_KEY], int):
        # SKIP LOW DEPTH POSITIONS
        if sample_vcf[DEPTH_KEY] < int(options.mindepth):
            return(False)
    #multi depth field
    else:
        # SKIP LOW DEPTH POSITIONS
        if sum(sample_vcf[DEPTH_KEY]) < int(options.mindepth):
            return(False)
    return(True)

def check_vaf(sample_vcf):
    #single depth field
    if isinstance(sample_vcf[DEPTH_KEY], int):
        # CHECK VAF
        if (sum(sample_vcf[VAF_KEY][1:])*1.0/sample_vcf[DEPTH_KEY]) < float(options.minvaf):
            return(False)
    #multi depth field
    else:
        # CHECK VAF
        if (sum(sample_vcf[VAF_KEY][1:])*1.0/sum(sample_vcf[DEPTH_KEY])) < float(options.minvaf):
            return(False)
    return(True)

# -------------------------------------------------
# RESTfull functions
def generic_json_request_handler(server, ext):
    r = requests.get(server+ext, headers={ "Content-Type" : "application/json"})
    if not r.ok:
        r.raise_for_status()
        sys.exit()

    return(r.json())


def get_geneinfo(gene, idtype):
    server = "https://grch37.rest.ensembl.org"

    if idtype == "symbol":
        ext = "/lookup/symbol/homo_sapiens/{}?content-type=application/json".format(gene)
    else:
        ext = "/lookup/id/{}?content-type=application/json".format(gene)

    json = generic_json_request_handler(server, ext)
    genedef = {"Chr":json['seq_region_name'], "Start":json['start'], "Stop":json['end'], "SYMBOL":json['display_name'], "ENSEMBLID":json['id']}

    return(genedef)


def get_canonical(ensembleid):
    server = "https://grch37.rest.ensembl.org"
    ext = "/lookup/id/{}?content-type=application/json;expand=1;db_type=core".format(ensembleid)
    json = generic_json_request_handler(server, ext)

    for i in range(0,len(json["Transcript"])):
        if json['Transcript'][i]['is_canonical'] == 1:
            return(json['Transcript'][i]['id'])

    # if there is no canonical just take the first
    print("[WARN]   No cannonical transcript found for gene {}, taking the first transcript".format(ensembleid))
    return(json['Transcript'][0]['id'])

# -------------------------------------------------

def main():
    global DEPTH_KEY
    global VAF_KEY

    file_list = glob.glob(os.path.join(options.vcfdir, "*.vcf"))
    for vcf_file in file_list:
        zip_and_index(vcf_file)


    genelist=[]

    # We only want to run this once per genelist, faster and kinder
    if not os.path.isfile(options.genelist+".pkl"):
        if debug: print("GENERATING ENSEMBL GENELIST")
        genecollection=[]
        with open(options.genelist, 'r') as infile:
            for line in infile:
                genesymbol = line.strip().split('\t')[3]

                if genesymbol not in genecollection:
                    genelist.append(get_geneinfo(genesymbol, 'symbol'))
                    genecollection.append(genesymbol)

        f = open(options.genelist+".pkl","wb")
        pickle.dump(genelist,f)
        f.close()
    else:
        with open(options.genelist+".pkl", 'rb') as handle:
            genelist = pickle.load(handle)

    if debug: print("GENES {}".format(genelist))

    # DF to keep the mutation effcts per gene
    df = {}
    #VCF record df, for MAX effects only, used for lollipop data
    rdf= {}
    #Count data frame
    cdf = {}

    # FOR ALL VCF FILES
    for vcf_file in file_list:
        if (debug):
            print("------")
            print(vcf_file)
        vcfread = vcf.Reader(open(vcf_file+".gz",'r'), compressed="gz")

        if (debug): print(vcfread.samples)
        if (debug): print(options.format)

        # FOR EACH SAMPLE
        for i,sample in enumerate(vcfread.samples):
            samplename = False

            if options.format == "GATK":
                samplename = sample
            elif options.format == "FREEB":
                if (debug): print("++ "+vcfread.samples[1])
                samplename = vcfread.samples[i+1]
                #samplename = vcf_file.split(".")[1].split("_")[1]
            df[samplename] = {}
            rdf[samplename] = {}
            cdf[samplename] = {}

        if debug: print(df)

        # FOR EACH GENE OF INTREST
        for thisgene in genelist:
            nr_of_positions = 0
            if len(thisgene)<=0:
                continue

            #if debug: print(")
            vcf_records=False
            try:
                vcf_records = vcfread.fetch(thisgene["Chr"], int(thisgene["Start"])-20, int(thisgene["Stop"])+20)
            except ValueError as e:
                if debug: print("-- {}\tNO RECORDS FOUND".format(thisgene))
                for samplename in df:
                    df[samplename][thisgene["SYMBOL"]] = "None"
                continue

            # Prep containers
            effects = {}
            records = {}
            for samplename in df:
                effects[samplename] = []
                records[samplename] = []

            # For each variant position within gene
            for vcf_record in vcf_records:
                if debug: print("@@@\t {}".format(vcf_record.INFO))

                if not "ANN" in vcf_record.INFO:
                    if debug: print("@@@\t skipping record {} due to missing ANN field".format(vcf_record))
                    continue


                gencheck = [thisgene["SYMBOL"] in a for a in vcf_record.INFO["ANN"]]
                if sum(gencheck) <= 0:
                    if debug: print("@@@\t skipping record {} due to missing GENE SYMBOL {}".format(vcf_record, thisgene["SYMBOL"]))
                    continue

                nr_of_positions += 1
                # For each sample
                for samplename in df:
                    #CHECK IF SAMPLE GENOTYPE AVAILABLE
                    sgenot = None
                    try:
                        sgenot = vcf_record.genotype(samplename)
                        #if debug: print("-- {}\t{}\t{}\tGT FOUND".format(thisgene, samplename, sgenot))
                    except AttributeError as e:
                        #if debug: print("-- {}\t{}\tNO GT FOUND".format(thisgene, samplename))
                        continue

                    # FILTER NON-QC RECORDS
                    PASS = False
                    log = "++ {}\t{}\t{}\t{}".format(thisgene, samplename, vcf_record, vcf_record.genotype(samplename)['GT'])
                    # CHEK IF AD FIELD PRESENT
                    if check_ad(sgenot):
                        log += "\tAD:PASS"
                        log += "\tDEPTH:{}".format(vcf_record.genotype(samplename)[DEPTH_KEY])
                        # CHECK TOTAL COVERAGE OF IDENTIFIED ALLELLES
                        if check_depth(sgenot):
                            log += ":PASS"
                            log += "\tVAF:{}".format(sum(vcf_record.genotype(samplename)[VAF_KEY][1:])*1.0/sum(vcf_record.genotype(samplename)[DEPTH_KEY]))

                            # add clean if sufficient depth is measured
                            effects[samplename].append("clean")
                            records[samplename].append(None)

                            # CHECK VARIANT ALLELE FREQUENCY
                            if check_vaf(sgenot):
                                log +=":PASS"
                                log +="\tPOP:{}".format([vcf_record.INFO[rf] for rf in FREQ_FIELDS if rf in vcf_record.INFO])
                                # CHECK POPULATION FREQUENCY
                                if max(find_popfreq(vcf_record)) <= float(options.popfreq):
                                    log += ":PASS"
                                    log += "\tMLEAF:{}".format(vcf_record.INFO["MLEAF"])
                                    # CHECK OCCURENCE IN TOTAL POOL
                                    if max(vcf_record.INFO["MLEAF"]) <= float(options.cohfreq):
                                        log +=":PASS"
                                        PASS = True

                    if debug: print(log)
                    if PASS:
                        # PARSE '0/1' into ALT[0] or '0/2' into ALT[1]
                        sample_call = sgenot['GT'].replace("|","").split("/")
                        sample_gt = vcf_record.ALT[int(sample_call[-1])-1]
                        #if debug: print("-- {}\t{}\tPARSED GT\t{}\t{}\t{}".format(thisgene, samplename, sgenot, sample_call, sample_gt))

                        effects[samplename].append(find_effects(vcf_record, sample_gt))
                        #print("SAMPLE: {} \t\t EFF: {}".format(samplename,effects[samplename]))
                        records[samplename].append(vcf_record)

            #exit(0)
            # ON GENE+SAMPLE LEVEL determine the number of mutations and the maximum mutation effect
            for samplename in df:
                # If no murtations/effects measured consider the gene as 'not assesed'
                if len(effects[samplename]) <= 0:
                    df[samplename][thisgene["SYMBOL"]] = "None"
                    cdf[samplename][thisgene["SYMBOL"]] = 0

                # Else determine the max effect
                else:
                    cdf[samplename][thisgene["SYMBOL"]] = sum([eff in toselect for eff in effects[samplename]])
                    #len(effects[samplename]) - effects[samplename].count("clean")
                    loc = select_maximum_effect(effects[samplename])
                    eff = effects[samplename][loc]

                    # If a 'strong enough' effect is detected report it in the summary
                    if eff in toselect:
                        df[samplename][thisgene["SYMBOL"]] = eff
                        if eff in mapping:
                            rdf[samplename][thisgene["SYMBOL"]] = {}
                            rdf[samplename][thisgene["SYMBOL"]]["REC"] = records[samplename][loc]
                            rdf[samplename][thisgene["SYMBOL"]]["EFF"] = eff

                    # Else check if gene was not observed 'None' or not mutated 'clean'
                    else:
                        # check number of 'clean' positions
                        # if 50% of positions passes DP metric count as clean
                        if effects[samplename].count("clean") >= (nr_of_positions/2):
                            df[samplename][thisgene["SYMBOL"]] = "clean"
                        else:
                            df[samplename][thisgene["SYMBOL"]] = "None"

                if debug: print("** {}\t{}\t{}\t{}\t{}".format(thisgene, samplename, df[samplename][thisgene["SYMBOL"]], cdf[samplename][thisgene["SYMBOL"]], ",".join(effects[samplename])))


    # Printing the mutation overview table
    outfile = open(options.outdir+"/"+"MutationOverview.txt",'w')
    # Print header with gene names
    if debug: print(df)
    firstsample = list(df.keys())[0]
    outfile.write("Sample\t{}\n".format('\t'.join(df[firstsample].keys()) ))
    if debug: print("##############################")
    # Loop all samples
    for sp in df:
        if debug: print("{}\t{}\n".format(sp, '\t'.join(df[sp].values()) ))
        outfile.write("{}\t{}\n".format(sp, '\t'.join(df[sp].values()) ))

    if debug: print("##############################")
    outfile.close()


    # Printing the mutation count table
    outfile = open(options.outdir+"/"+"MutationCounts.txt",'w')
    # Print header with gene names
    firstsample = list(cdf.keys())[0]
    outfile.write("Sample\t{}\tTotMutCount\n".format('\t'.join(cdf[firstsample].keys()) ))
    if debug: print("##############################")
    # Loop all samples
    for sp in cdf:
        if debug: print("{}\t{}\t{}\n".format(sp, '\t'.join([str(i) for i in cdf[sp].values()]), sum(cdf[sp].values()) ))
        outfile.write("{}\t{}\t{}\n".format(sp, '\t'.join([str(i) for i in cdf[sp].values()]), sum(cdf[sp].values()) ))

    if debug: print("##############################")
    outfile.close()


    # Printing the mutation details chart/table
    outfile = open(options.outdir+"/"+"MutationChart.txt",'w')
    # Printing annotations header
    outfile.write("{}\n".format('\t'.join(lollipop)))

    if debug: print("##############################")
    for samplename in rdf:
        for gene in rdf[samplename]:
            thisrec = rdf[samplename][gene]["REC"]

            vaf=round((sum(thisrec.genotype(samplename)[VAF_KEY][1:])*1.0)/sum(thisrec.genotype(samplename)[DEPTH_KEY]),2)

            sample_call = thisrec.genotype(samplename)['GT'].replace("|","").split("/")
            #print(sample_call)
            #print(sample_call[-1])
            #print(thisrec.ALT)
            sample_gt = thisrec.ALT[int(sample_call[-1])-1]

            proteffect=None
            for pred in thisrec.INFO["ANN"]:
                # Look for the first transcript with this effect
                if rdf[samplename][gene]["EFF"] in pred.split("|")[1].split("&"):
                    proteffect=pred.split("|")[10]
                    break

            if (debug): print(gene, samplename, proteffect, mapping[rdf[samplename][gene]["EFF"]], str(thisrec.CHROM), str(thisrec.POS), str(thisrec.POS+len(thisrec.ALT[0])), thisrec.REF, str(thisrec.ALT[0]), vaf)

            outfile.write("\t".join([gene, samplename, proteffect, mapping[rdf[samplename][gene]["EFF"]], str(thisrec.CHROM), str(thisrec.POS), str(thisrec.POS+len(sample_gt)), thisrec.REF, str(sample_gt), str(vaf)])+"\n")
    if debug: print("##############################")
    outfile.close()




# -------------------------------------------------

print("Starting Analysis")

if __name__ == '__main__':
    if check_arguments():
        main()
    else:
        print("Error in provided arguments")

print("DONE")

# -------------------------------------------------
