# EAR 👂 (ERGA Assembly Report)

Here you will find assembly reports approved by the [ERGA Sequencing and Assembly Committee](https://www.erga-biodiversity.eu/team-1/sac---sequencing-and-assembly-committee) and instructions on how to create one for getting your assembly assessed.

### Reviewing your assembly before curation

After completing the [assembly pipeline](https://github.com/ERGA-consortium/pipelines) to obtain the final pre-curated assembly of your species of interest, please generate the corresponding ERGA Assembly Report (EAR) to get the _go-ahead_ to continue with the manual curation process.

The report is a PDF file showing relevant metrics during the stages of the assembly process that reviewers will use to confirm that the assembly meets the [EBP quality metrics]().

Which results do I need for the EAR?
- [Genomescope](https://github.com/tbenavi1/genomescope2.0) and [Smudgeplot](https://github.com/KamilSJaron/smudgeplot) from your WGS accurate reads.
- [gfastats](https://github.com/vgl-hub/gfastats), [Merqury](https://github.com/marbl/merqury) and [BUSCO](https://gitlab.com/ezlab/busco) results at every assembly step (for both -pseudo-haplotypes, if available).
- HiC contact maps for the scaffolding step(s) (for both -pseudo-haplotypes, if available).


## Getting there is a two-part job:

### 1. Creating your EAR

We provide three ways to produce the EAR:
1. If you have the required results, you can complete the YAML file (take a look at [EAR_basic_template.yaml](EAR_basic_template.yaml) and [[example]rCarCar2_EAR.yaml]([example]rCarCar2_EAR.yaml) files) and run the `make_EAR.py` script. We recommend installing the provided conda environment to handle the program's requirements easily. Clone this repository to obtain all the required files.

```bash
# Complete the YAML file for your species

# Creating the EAR environment to run the script
conda env create -f EAR_env.yml

# Running the script to obtain the EAR pdf
python make_EAR.py mySpecies_EAR.yaml
```

2. [in preparation] Using the [Galaxy ERGA Assembly Review (EAR) Analysis + Report workflow]() to run all the QC analysis on the different stages of the assembly pipeline and get the report at the end.
If you are not already using Galaxy to produce your genome assembly, you will need to "create Galaxy account" (with enough space, you can ask) and upload all your assemblies and reads (WGS accurate reads for Kmer database creation and HiC for contact map).
3. [in preparation] Using the [snakemake-based tool GEP](). By means of this pipeline, you can run all the analysis in one take and obtain the YAML file to immediately run the `make_EAR.py` script (before running the script, you would only need to edit the YAML file to enter information like affiliation, etc.).


### 2. Getting your assembly reviewed using the EAR

Fork this repository and create a folder and subfolder with your species name and ToLID, respectively, and place your EAR pdf inside, e.g., `Caretta_caretta/rCarCar2/rCarCar2_EAR.pdf`.

[image]

Create a pull request so the reviewers can assess your assembly using the EAR.

[image]

Once the pull request is placed, two independent reviewers will evaluate your report, and a channel of communication will be established based on this.

[image]

If everything looks good, the assembly will be approved to continue to the manual curation stage, and the EAR will be part of the stable ERGA repository of reports.

If the reviewer thinks something should be clarified, addressed or corrected, it will be requested through the communication channel open during the pull request.

[image]
