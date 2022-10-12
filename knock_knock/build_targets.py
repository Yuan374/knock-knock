import logging
import shutil
import subprocess
import sys
import warnings

from urllib.parse import urlparse
from pathlib import Path

import pandas as pd
import numpy as np
import pysam
import yaml

import Bio.SeqIO
import Bio.SeqUtils
from Bio import BiopythonWarning
from Bio.SeqFeature import SeqFeature, FeatureLocation
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord

from hits import fastq, mapping_tools, sam, genomes, utilities, sw
from knock_knock import target_info, pegRNAs

def design_amplicon_primers_from_csv(base_dir, genome='hg19'):
    base_dir = Path(base_dir)
    csv_fn = base_dir / 'targets' / 'sgRNAs.csv'

    index_locations = target_info.locate_supplemental_indices(base_dir)
    if genome not in index_locations:
        print(f'Error: can\'t locate indices for {genome}')
        sys.exit(1)

    df = pd.read_csv(csv_fn).replace({np.nan: None})

    amplicon_primer_info = {}

    for _, row in df.iterrows():
        name = row['name']
        logging.info(f'Designing {name}...')
        best_candidate = design_amplicon_primers(base_dir, row, genome)
        if best_candidate is not None:
            amplicon_primer_info[name] = {
                'flanking_sequence': best_candidate['target_seq'],
                'genome': genome,
                'ref_name': best_candidate['ref_name'],
                'min_cut_after': best_candidate['min_cut_after'],
                'max_cut_after': best_candidate['max_cut_after'],
            }

    amplicon_primer_info = pd.DataFrame(amplicon_primer_info).T
    amplicon_primer_info.index.name = 'name'

    column_order = ['flanking_sequence', 'genome', 'ref_name', 'min_cut_after', 'max_cut_after']
    amplicon_primer_info = amplicon_primer_info[column_order]

    final_csv_fn = base_dir / 'targets' / 'sgRNAs_flanking_sequence.csv'
    amplicon_primer_info.to_csv(final_csv_fn)

def design_amplicon_primers(base_dir, info, genome):
    base_dir = Path(base_dir)

    name = info['name']

    target_dir = base_dir / 'targets' / name
    target_dir.mkdir(parents=True, exist_ok=True)

    protospacer, *other_protospacers = info['sgRNA_sequence'].upper().split(';')
    
    protospacer_dir = target_dir / 'protospacer_alignment'
    protospacer_dir.mkdir(exist_ok=True)
    fastq_fn = protospacer_dir / 'protospacer.fastq'
    STAR_prefix = protospacer_dir / 'protospacer_'
    bam_fn = protospacer_dir / 'protospacer.bam'

    index_locations = target_info.locate_supplemental_indices(base_dir)
    STAR_index = index_locations[genome]['STAR']

    # Make a fastq file with a single read containing the protospacer sequence.
    
    with fastq_fn.open('w') as fh:
        quals = fastq.encode_sanger([40]*len(protospacer))
        read = fastq.Read('protospacer', protospacer, quals)
        fh.write(str(read))
        
    # Align the protospacer to the reference genome.
    mapping_tools.map_STAR(fastq_fn, STAR_index, STAR_prefix, mode='guide_alignment', bam_fn=bam_fn, sort=False)

    with pysam.AlignmentFile(bam_fn) as bam_fh:
        perfect_als = [al for al in bam_fh if not al.is_unmapped and sam.total_edit_distance(al) == 0]
    
    region_fetcher = genomes.build_region_fetcher(index_locations[genome]['fasta'])

    def evaluate_candidate(al):
        results = {
            'location': f'{al.reference_name} {al.reference_start:,} {sam.get_strand(al)}',
            'ref_name': al.reference_name,
            'cut_afters': [],
        }

        full_window_around = 5000

        full_around = region_fetcher(al.reference_name, al.reference_start - full_window_around, al.reference_end + full_window_around).upper()

        if sam.get_strand(al) == '+':
            ps_seq = protospacer
            ps_strand = 1
        else:
            ps_seq = utilities.reverse_complement(protospacer)
            ps_strand  = -1
        
        ps_start = full_around.index(ps_seq)

        protospacer_locations = [(ps_seq, ps_start, ps_strand)]

        for other_protospacer in other_protospacers:
            if other_protospacer in full_around:
                ps_seq = other_protospacer
                ps_strand = 1
            else:
                ps_seq =  utilities.reverse_complement(other_protospacer)
                if ps_seq not in full_around:
                    results['failed'] = f'protospacer {other_protospacer} not present near protospacer {protospacer}'
                    return results
                ps_strand = -1

            ps_start = full_around.index(ps_seq)
            protospacer_locations.append((ps_seq, ps_start, ps_strand))

        for ps_seq, ps_start, ps_strand in protospacer_locations:
            if ps_strand == 1:
                PAM_offset = len(protospacer)
                PAM_transform = utilities.identity
                cut_after = al.reference_start - full_window_around + ps_start + PAM_offset - 3
            else:
                PAM_offset = -3
                PAM_transform = utilities.reverse_complement
                cut_after = al.reference_start - full_window_around + ps_start + 2

            results['cut_afters'].append(cut_after)

            PAM_start = ps_start + PAM_offset
            PAM = PAM_transform(full_around[PAM_start:PAM_start + 3])
            pattern, *matches = Bio.SeqUtils.nt_search(PAM, 'NGG')

            if 0 not in matches:
                # Note: this could incorrectly fail if there are multiple exact matches for an other_protospacer
                # in full_around.
                results['failed'] = f'bad PAM: {PAM} next to {ps_seq} (strand {ps_strand})'
                return results

        min_start = min(ps_start for ps_seq, ps_start, ps_strand in protospacer_locations)
        max_start = max(ps_start for ps_seq, ps_start, ps_strand in protospacer_locations)

        results['min_cut_after'] = min(results['cut_afters'])
        results['max_cut_after'] = max(results['cut_afters'])
        
        final_window_around = 500    

        final_start = min_start - final_window_around
        final_end = max_start + final_window_around

        target_seq = full_around[final_start:final_end]
        results['target_seq'] = target_seq

        return results

    good_candidates = []
    bad_candidates = []
    
    for al in perfect_als:
        results = evaluate_candidate(al)
        if 'failed' in results:
            bad_candidates.append(results)
        else:
            good_candidates.append(results)

    if len(good_candidates) == 0:
        if len(bad_candidates) == 0:
            print(f'Error building {name}: no perfect matches to sgRNA {protospacer} found in {genome}')
            return 

        else:
            print(f'Error building {name}: no valid genomic locations for {name}')

            for results in bad_candidates:
                print(f'\t{results["location"]}: {results["failed"]}')

            return 

    elif len(good_candidates) > 1:
        print(f'Warning: multiple valid genomic locations for {name}:')
        for results in good_candidates:
            print(f'\t{results["location"]}')
        best_candidate = good_candidates[0]
        print(f'Arbitrarily choosing {best_candidate["location"]}')
    else:
        best_candidate = good_candidates[0]

    return best_candidate

def identify_homology_arms(donor_seq, donor_type, target_seq, cut_after, required_match_length=15):
    header = pysam.AlignmentHeader.from_references(['donor', 'target'], [len(donor_seq), len(target_seq)])
    mapper = sw.SeedAndExtender(donor_seq.encode(), 8, header, 'donor')
    
    target_bytes = target_seq.encode()
    
    alignments = {
        'before_cut': [],
        'after_cut': [],
    }

    seed_starts = {
        'before_cut': range(cut_after - required_match_length, 0, -1),
        'after_cut': range(cut_after, len(target_seq) - required_match_length),
    }

    for side in ['before_cut', 'after_cut']:
        for seed_start in seed_starts[side]:  
            alignments[side] = mapper.seed_and_extend(target_bytes, seed_start, seed_start + required_match_length, 'target')
            if alignments[side]:
                break

        else:
            results = {'failed': f'cannot locate homology arm on {side}'}
            return results
        
    possible_HA_boundaries = []
    
    for before_al in alignments['before_cut']:
        for after_al in alignments['after_cut']:
            if sam.get_strand(before_al) == sam.get_strand(after_al):
                strand = sam.get_strand(before_al)
                if strand == '+':
                    if before_al.reference_end < after_al.reference_start:
                        possible_HA_boundaries.append((donor_seq, before_al.reference_start, after_al.reference_end))
                elif strand == '-':
                    if before_al.reference_start > after_al.reference_end:
                        flipped_seq = utilities.reverse_complement(donor_seq)
                        start = len(donor_seq) - 1 - (before_al.reference_end - 1)
                        end = len(donor_seq) - 1 - after_al.reference_start + 1
                        possible_HA_boundaries.append((flipped_seq, start, end))

    possible_HAs = []
    for possibly_flipped_donor_seq, HA_start, HA_end in possible_HA_boundaries:
        donor_window = possibly_flipped_donor_seq[HA_start:HA_end]

        donor_prefix = donor_window[:required_match_length]

        donor_suffix = donor_window[-required_match_length:]

        # Try to be resilient against multiple occurrence of HA substrings in the target
        # by prioritizing matches closest to the cut site.
        target_HA_start = target_seq.rfind(donor_prefix, 0, cut_after + required_match_length)
        target_HA_end = target_seq.find(donor_suffix, cut_after - required_match_length) + len(donor_suffix)

        if target_HA_start == -1 or target_HA_end == -1 or target_HA_start >= target_HA_end:
            results = {'failed': f'cannot locate homology arms in target'}
            return results

        relevant_target_seq = target_seq[target_HA_start:target_HA_end]

        total_HA_length = target_HA_end - target_HA_start

        mismatches_before_deletion = np.cumsum([t != d for t, d in zip(relevant_target_seq, donor_window)])

        flipped_target = relevant_target_seq[::-1]
        flipped_donor = donor_window[::-1]
        mismatches_after_deletion = np.cumsum([0] + [t != d for t, d in zip(flipped_target, flipped_donor)][:-1])[::-1]

        total_mismatches = mismatches_before_deletion + mismatches_after_deletion

        last_index_in_HA_1 = int(np.argmin(total_mismatches))
        min_mismatches = total_mismatches[last_index_in_HA_1]

        lengths = {}
        lengths['HA_1'] = last_index_in_HA_1 + 1
        lengths['HA_2'] = total_HA_length - lengths['HA_1']
        lengths['donor_specific'] = len(donor_seq) - total_HA_length
        
        info = {
            'min_mismatches': min_mismatches,
            'possibly_flipped_donor_seq': possibly_flipped_donor_seq,
            'donor_HA_start': HA_start,
            'donor_HA_end': HA_end,
            'target_HA_start': target_HA_start,
            'target_HA_end': target_HA_end,
            'lengths': lengths,
        }
        possible_HAs.append((info))
        
    def priority(info):
        return info['min_mismatches'], -min(info['lengths']['HA_1'], info['lengths']['HA_2'])

    if not possible_HAs:
        results = {'failed': 'cannot locate homology arms'}
    else:
        results = min(possible_HAs, key=priority)

    lengths = results['lengths']

    donor_starts = {
        'HA_1': results['donor_HA_start'],
        'donor_specific': results['donor_HA_start'] + lengths['HA_1'],
        'HA_2': results['donor_HA_end'] - lengths['HA_2'],
    }
    donor_ends = {
        'HA_1': donor_starts['HA_1'] + lengths['HA_1'],
        'donor_specific': donor_starts['HA_2'],
        'HA_2': donor_starts['HA_2'] + lengths['HA_2'],
    }

    if donor_type == 'PCR':
        if donor_starts['HA_1'] != 0:
            donor_starts['PCR_adapter_1'] = 0
            donor_ends['PCR_adapter_1'] = donor_starts['HA_1']

        if donor_ends['HA_2'] != len(donor_seq):
            donor_starts['PCR_adapter_2'] = donor_ends['HA_2']
            donor_ends['PCR_adapter_2'] = len(donor_seq)

    target_starts = {
        'HA_1': results['target_HA_start'],
        'HA_2': results['target_HA_end'] - lengths['HA_2'],
    }
    target_ends = {key: target_starts[key] + lengths[key] for key in target_starts}

    donor_strand = 1
    target_strand = 1

    donor_features = [
        SeqFeature(location=FeatureLocation(donor_starts[feature_name], donor_ends[feature_name], strand=donor_strand),
                    id=feature_name,
                    type='misc_feature',
                    qualifiers={'label': feature_name,
                                'ApEinfo_fwdcolor': feature_colors[feature_name],
                               },
                  )
        for feature_name in donor_starts
    ]

    target_features = ([
        SeqFeature(location=FeatureLocation(target_starts[feature_name], target_ends[feature_name], strand=target_strand),
                    id=feature_name,
                    type='misc_feature',
                    qualifiers={'label': feature_name,
                                'ApEinfo_fwdcolor': feature_colors[feature_name],
                               },
                  )
        for feature_name in target_starts
    ])

    HA_info = {
        'possibly_flipped_donor_seq': results['possibly_flipped_donor_seq'],
        'donor_features': donor_features,
        'target_features': target_features,
    }

    return HA_info

feature_colors = {
    'HA_1': '#c7b0e3',
    'HA_RT': '#c7b0e3',
    'RTT': '#c7b0e3',
    'HA_2': '#85dae9',
    'HA_PBS': '#85dae9',
    'PBS': '#85dae9',
    'forward_primer': '#75C6A9',
    'reverse_primer': '#9eafd2',
    'sgRNA': '#c6c9d1',
    'donor_specific': '#b1ff67',
    'PCR_adapter_1': '#F8D3A9',
    'PCR_adapter_2': '#D59687',
    'protospacer': '#ff9ccd',
    'scaffold': '#b7e6d7',
}

def build_target_info(base_dir, info, all_index_locations,
                      defer_HA_identification=False,
                      offtargets=False,
                     ):
    ''' 
    Attempts to identify the genomic location where an sgRNA sequence
    is flanked by amplicon primers.
    
    info should have keys:
        genome
        sgRNA_sequence
        amplicon_primers
    optional keys:
        pegRNAs
        donor_sequence
        nonhomologous_donor_sequence
        extra_sequences
        effector
    '''
    genome = info['genome']
    if info['genome'] not in all_index_locations:
        print(f'Error: can\'t locate indices for {genome}')
        sys.exit(1)
    else:
        index_locations = all_index_locations[genome]

    base_dir = Path(base_dir)

    name = info['name']

    donor_info = info.get('donor_sequence')
    if donor_info is None:
        donor_name = None
        donor_seq = None
    else:
        donor_name, donor_seq = donor_info
        if donor_name is None:
            donor_name = f'{name}_donor'

    if donor_seq is None:
        has_donor = False
    else:
        has_donor = True

    if info['donor_type'] is None:
        donor_type = None
    else:
        _, donor_type = info['donor_type']

    nh_donor_info = info.get('nonhomologous_donor_sequence')
    if nh_donor_info is None:
        nh_donor_name = None
        nh_donor_seq = None
    else:
        nh_donor_name, nh_donor_seq = nh_donor_info
        if nh_donor_name is None:
            nh_donor_name = f'{name}_NH_donor'

    if nh_donor_seq is None:
        has_nh_donor = False
    else:
        has_nh_donor = True

    if 'effector' in info:
        effector_type = info['effector']
    else:
        if donor_type == 'pegRNA':
            effector_type = 'SpCas9H840A'
        else:
            effector_type = 'SpCas9'

    if info.get('pegRNAs') is not None:
        pegRNA_names = [pegRNA_name for pegRNA_name, components in sorted(info['pegRNAs']) ]
    else:
        pegRNA_names = []

    effector = target_info.effectors[effector_type]

    target_dir = base_dir / 'targets' / name
    target_dir.mkdir(parents=True, exist_ok=True)
    
    protospacer, *other_protospacers = info['sgRNA_sequence']
    primers_name, primers = info['amplicon_primers']
    primers = primers.split(';')

    if primers_name is None:
        target_name = name
    else:
        target_name = primers_name

    protospacer_dir = target_dir / 'protospacer_alignment'
    protospacer_dir.mkdir(exist_ok=True)
    fastq_fn = protospacer_dir / 'protospacer.fastq'
    STAR_prefix = protospacer_dir / 'protospacer_'
    bam_fn = protospacer_dir / 'protospacer.bam'

    STAR_index = index_locations['STAR']

    # Make a fastq file with a single read containing the protospacer sequence.
    protospacer_name, protospacer_seq = protospacer
    
    with fastq_fn.open('w') as fh:
        quals = fastq.encode_sanger([40]*len(protospacer_seq))
        read = fastq.Read('protospacer', protospacer_seq, quals)
        fh.write(str(read))
        
    # Align the protospacer to the reference genome.
    mapping_tools.map_STAR(fastq_fn, STAR_index, STAR_prefix, mode='guide_alignment', bam_fn=bam_fn)
    
    with pysam.AlignmentFile(bam_fn) as bam_fh:
        perfect_als = []
        imperfect_als = []
        for al in bam_fh:
            if not al.is_unmapped:
                if sam.total_edit_distance(al) == 0:
                    perfect_als.append(al)
                else:
                    # Consider alignments with mismatching first base (presumably due to prepended G) perfect.
                    nonmatching_ps = {true_read_i for (true_read_i, read_b, _, ref_b, _) in sam.aligned_tuples(al) if read_b != ref_b}
                    if nonmatching_ps == {0}:
                        perfect_als.append(al)
                    else:
                        imperfect_als.append(al)
    
    region_fetcher = genomes.build_region_fetcher(index_locations['fasta'])
    
    def evaluate_candidate(al):
        results = {
            'location': f'{al.reference_name} {al.reference_start:,} {sam.get_strand(al)}',
        }

        full_window_around = 5000

        full_around = region_fetcher(al.reference_name, al.reference_start - full_window_around, al.reference_end + full_window_around).upper()

        if sam.get_strand(al) == '+':
            ps_seq = protospacer_seq
            ps_strand = 1

            if ps_seq not in full_around:
                # Initial base mismatches.
                ps_seq = ps_seq[1:]

        else:
            ps_seq = utilities.reverse_complement(protospacer_seq)
            ps_strand  = -1

            if ps_seq not in full_around:
                # Initial base mismatches.
                ps_seq = ps_seq[:-1]

        ps_start = full_around.index(ps_seq)

        protospacer_locations = [(protospacer_name, ps_seq, ps_start, ps_strand)]

        for other_protospacer_name, other_protospacer_seq in other_protospacers:

            # Initial G may not match genome.
            if other_protospacer_seq.startswith('G'):
                other_protospacer_seq = other_protospacer_seq[1:]

            if other_protospacer_seq in full_around:
                ps_seq = other_protospacer_seq
                ps_strand = 1
            else:
                ps_seq =  utilities.reverse_complement(other_protospacer_seq)
                if ps_seq not in full_around:
                    results['failed'] = f'protospacer {other_protospacer_name}: {other_protospacer_seq} not present near protospacer {protospacer_seq}'
                    return results
                ps_strand = -1

            ps_start = full_around.index(ps_seq)
            protospacer_locations.append((other_protospacer_name, ps_seq, ps_start, ps_strand))

        for ps_name, ps_seq, ps_start, ps_strand in protospacer_locations:
            PAM_pattern = effector.PAM_pattern

            if (ps_strand == 1 and effector.PAM_side == 3) or (ps_strand == -1 and effector.PAM_side == 5):
                PAM_offset = len(ps_seq)
                PAM_transform = utilities.identity
            else:
                PAM_offset = -len(PAM_pattern)
                PAM_transform = utilities.reverse_complement

            PAM_start = ps_start + PAM_offset
            PAM = PAM_transform(full_around[PAM_start:PAM_start + len(PAM_pattern)])
            pattern, *matches = Bio.SeqUtils.nt_search(PAM, PAM_pattern)

            if 0 not in matches and not offtargets:
                # Note: this could incorrectly fail if there are multiple exact matches for an other_protospacer
                # in full_around.
                results['failed'] = f'bad PAM: {PAM} next to {ps_seq} (strand {ps_strand})'
                return results

        if primers[0] in full_around:
            leftmost_primer = primers[0]
            rightmost_primer = utilities.reverse_complement(primers[1])
            if rightmost_primer not in full_around:
                results['failed'] = f'primer {primers[1]} not present near protospacer'
                return results
            
            leftmost_primer_name = 'forward_primer'
            rightmost_primer_name = 'reverse_primer'

        else:
            leftmost_primer = primers[1]
            rightmost_primer = utilities.reverse_complement(primers[0])

            if leftmost_primer not in full_around:
                results['failed'] = f'primer {primers[1]} not present near protospacer'
                return results

            if rightmost_primer not in full_around:
                results['failed'] = f'primer {primers[0]} not present near protospacer'
                return results

            leftmost_primer_name = 'reverse_primer'
            rightmost_primer_name = 'forward_primer'

        leftmost_start = full_around.index(leftmost_primer)
        rightmost_start = full_around.index(rightmost_primer)

        if leftmost_start >= rightmost_start:
            results['failed'] = f'primers don\'t flank protospacer'
            return results
        
        # Now that primers have been located, redefine the target sequence to include a fixed
        # window on either side of the primers.

        final_window_around = 500    

        offset = leftmost_start - final_window_around

        final_start = leftmost_start - final_window_around
        final_end = rightmost_start + len(rightmost_primer) + final_window_around

        target_seq = full_around[final_start:final_end]

        leftmost_location = FeatureLocation(leftmost_start - offset, leftmost_start - offset + len(leftmost_primer), strand=1)
        rightmost_location = FeatureLocation(rightmost_start - offset, rightmost_start - offset + len(rightmost_primer), strand=-1)
        
        target_features = [
            SeqFeature(location=leftmost_location,
                       id=leftmost_primer_name,
                       type='misc_feature',
                       qualifiers={'label': leftmost_primer_name,
                                   'ApEinfo_fwdcolor': feature_colors[leftmost_primer_name],
                                  },
                      ),
            SeqFeature(location=rightmost_location,
                       id=rightmost_primer_name,
                       type='misc_feature',
                       qualifiers={'label': rightmost_primer_name,
                                   'ApEinfo_fwdcolor': feature_colors[rightmost_primer_name],
                                  },
                      ),
        ]

        if leftmost_primer_name == 'forward_primer':
            start = leftmost_start - offset
            start_location = FeatureLocation(start, start + 5, strand=1)
        else:
            start = rightmost_start - offset + len(rightmost_primer) - 5
            start_location = FeatureLocation(start, start + 5, strand=-1)

        target_features.extend([
            SeqFeature(location=start_location,
                       id='sequencing_start',
                       type='misc_feature',
                       qualifiers={
                           'label': 'sequencing_start',
                       },
                      ),
            SeqFeature(location=start_location,
                       id='anchor',
                       type='misc_feature',
                       qualifiers={
                           'label': 'anchor',
                       },
                      ),
        ])

        sgRNA_features = []
        for sgRNA_i, (ps_name, ps_seq, ps_start, ps_strand) in enumerate(protospacer_locations):
            if ps_name in pegRNA_names:
                continue

            sgRNA_feature = SeqFeature(location=FeatureLocation(ps_start - offset, ps_start - offset + len(ps_seq), strand=ps_strand),
                                       id=ps_name,
                                       type=f'sgRNA_{effector.name}',
                                       qualifiers={
                                           'label': ps_name,
                                           'ApEinfo_fwdcolor': feature_colors['sgRNA'],
                                       },
                                      )
            target_features.append(sgRNA_feature)
            sgRNA_features.append(sgRNA_feature)

        results['sgRNA_features'] = sgRNA_features

        results['gb_records'] = {}

        if has_donor:
            if not defer_HA_identification:
                # If multiple sgRNAs are given, the edited one must be listed first.
                sgRNA_feature = sgRNA_features[0]

                cut_after_offset = [offset for offset in effector.cut_after_offset if offset is not None][0]

                if sgRNA_feature.strand == 1:
                    # sgRNA_feature.end is the first nt of the PAM
                    cut_after = sgRNA_feature.location.end + cut_after_offset
                else:
                    # sgRNA_feature.start - 1 is the first nt of the PAM
                    cut_after = sgRNA_feature.location.start - 1 - cut_after_offset - 1

                HA_info = identify_homology_arms(donor_seq, donor_type, target_seq, cut_after)

                if 'failed' in HA_info:
                    results['failed'] = HA_info['failed']
                    return results

                donor_Seq = Seq(HA_info['possibly_flipped_donor_seq'])
                donor_features = HA_info['donor_features']
                target_features.extend(HA_info['target_features'])

            else:
                donor_Seq = Seq(donor_seq)
                donor_features = []

            donor_record = SeqRecord(donor_Seq, name=donor_name, features=donor_features, annotations={'molecule_type': 'DNA'})
            results['gb_records'][donor_name] = donor_record

        if info.get('pegRNAs') is not None:
            convert_strand = {
                '+': 1,
                '-': -1,
            }

            for pegRNA_name, pegRNA_components in info['pegRNAs']:
                pegRNA_features, new_target_features = pegRNAs.infer_features(pegRNA_name, pegRNA_components, target_name, target_seq)

                try:
                    pegRNA_SeqFeatures = [
                        SeqFeature(id=feature_name,
                                location=FeatureLocation(feature.start, feature.end + 1, strand=convert_strand[feature.strand]),
                                type='misc_feature',
                                qualifiers={
                                    'label': feature_name,
                                    'ApEinfo_fwdcolor': feature.attribute['color'],
                                },
                                )
                        for (_, feature_name), feature in pegRNA_features.items()
                    ]
                except:
                    for name, val in pegRNA_features.items():
                        print(name, val)
                    raise

                pegRNA_Seq = Seq(pegRNA_components['full_sequence'])
                pegRNA_record = SeqRecord(pegRNA_Seq,
                                          name=pegRNA_name,
                                          features=pegRNA_SeqFeatures,
                                          annotations={'molecule_type': 'DNA'},
                                         )
    
                results['gb_records'][pegRNA_name] = pegRNA_record
            
        if has_nh_donor:
            nh_donor_Seq = Seq(nh_donor_seq)
            nh_donor_record = SeqRecord(nh_donor_Seq, name=nh_donor_name, annotations={'molecule_type': 'DNA'})
            results['gb_records'][nh_donor_name] = nh_donor_record

        target_Seq = Seq(target_seq)
        target_record = SeqRecord(target_Seq, name=target_name, features=target_features, annotations={'molecule_type': 'DNA'})
        results['gb_records'][target_name] = target_record

        if info.get('pegRNAs') is not None:
            # Note: for debugging convenience, genbank files are written for pegRNAs,
            # but these are NOT supplied as genbank records to make the final TargetInfo,
            # since relevant features are either represented by the intial decomposition into
            # components or inferred on instantiation of the TargetInfo.
            non_pegRNA_records = {name: record for name, record in results['gb_records'].items() if name not in pegRNA_names}
            results['gb_records_for_manifest'] = non_pegRNA_records
        else:
            results['gb_records_for_manifest'] = results['gb_records']

        return results
    
    good_candidates = []
    bad_candidates = []
    
    for al in perfect_als:
        results = evaluate_candidate(al)
        if 'failed' in results:
            bad_candidates.append(results)
        else:
            good_candidates.append(results)
    
    if len(good_candidates) == 0:
        if len(bad_candidates) == 0:
            print(f'Error building {name}: no perfect matches to sgRNA {protospacer} found in {genome}')
            print(perfect_als)
            print(imperfect_als)
            return 

        else:
            print(f'Error building {name}: no valid genomic locations for {name}')

            for results in bad_candidates:
                print(f'\t{results["location"]}: {results["failed"]}')

            return 

    elif len(good_candidates) > 1:
        print(f'Warning: multiple valid genomic locations for {name}:')
        for results in good_candidates:
            print(f'\t{results["location"]}')
        best_candidate = good_candidates[0]
        print(f'Arbitrarily choosing {best_candidate["location"]}')
    else:
        best_candidate = good_candidates[0]

    truncated_name_i = 0
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=BiopythonWarning)

        for which_seq, record in best_candidate['gb_records'].items(): 
            gb_fn = target_dir / f'{which_seq}.gb'
            try:
                Bio.SeqIO.write(record, gb_fn, 'genbank')
            except ValueError:
                # locus line too long, can't write genbank file with BioPython
                old_name = record.name

                truncated_name = f'{record.name[:11]}_{truncated_name_i}'
                record.name = truncated_name
                Bio.SeqIO.write(record, gb_fn, 'genbank')

                record.name = old_name

                truncated_name_i += 1

    manifest_fn = target_dir / 'manifest.yaml'

    if info.get('extra_sequences') is not None:
        for extra_seq_name, extra_seq in info['extra_sequences']:
            record = SeqRecord(extra_seq, name=extra_seq_name, annotations={'molecule_type': 'DNA'})
            best_candidate['gb_records_for_manifest'][extra_seq_name] = record

    if info.get('extra_genbanks') is not None:
        for gb_fn in info['extra_genbanks']:
            full_gb_fn = base_dir / 'targets' / gb_fn

            if not full_gb_fn.exists():
                raise ValueError(f'{full_gb_fn} does not exist')

            for record in Bio.SeqIO.parse(full_gb_fn, 'genbank'):
                best_candidate['gb_records_for_manifest'][record.name] = record

    sources = sorted(best_candidate['gb_records_for_manifest'])
        
    manifest = {
        'sources': sources,
        'target': target_name,
    }
    if has_donor:
        manifest['donor'] = donor_name
        manifest['donor_specific'] = 'donor_specific'
        if donor_type is not None:
            manifest['donor_type'] = donor_type

    if has_nh_donor:
        manifest['nonhomologous_donor'] = nh_donor_name

    manifest['features_to_show'] = [
        [target_name, 'forward_primer'],
        [target_name, 'reverse_primer'],
    ]

    if has_donor:
        manifest['features_to_show'].extend([
            [donor_name, 'HA_1'],
            [donor_name, 'HA_2'],
            [donor_name, 'donor_specific'],
            [donor_name, 'PCR_adapter_1'],
            [donor_name, 'PCR_adapter_2'],
            [target_name, 'HA_1'],
            [target_name, 'HA_2'],
        ])

    manifest['genome_source'] = genome

    manifest_fn.write_text(yaml.dump(manifest, default_flow_style=False))

    if info.get('pegRNAs') is not None:
        # Make pegRNA components file within target dir.
        # ti necessary here just to get file name.
        # Need to set pegRNAs=[] to prevent a circular dependence
        # on the existence of the components file.
        ti = target_info.TargetInfo(base_dir, name, pegRNAs=[])
        pegRNAs_df = load_pegRNAs(base_dir, process=False)
        pegRNAs_df.loc[pegRNA_names].to_csv(ti.fns['pegRNAs'])

    gb_records = list(best_candidate['gb_records_for_manifest'].values())

    ti = target_info.TargetInfo(base_dir, name, gb_records=gb_records)

    ti.make_protospacer_fastas()
    ti.map_protospacers(genome)

    ti.identify_degenerate_indels()

    shutil.rmtree(protospacer_dir)

def load_pegRNAs(base_dir, process=True):
    '''
    If process == False, just pass along the DataFrame for subsetting.
    '''
    base_dir = Path(base_dir)
    csv_fn = base_dir / 'targets' / 'pegRNAs.csv'

    if not csv_fn.exists():
        return None
    else:
        return pegRNAs.read_csv(csv_fn, process=process)

def build_component_registry(base_dir):
    registry = {}

    sgRNA_fn = base_dir / 'targets' / 'sgRNAs.csv'

    if sgRNA_fn.exists():
        registry['sgRNA_sequence'] = pd.read_csv(sgRNA_fn, index_col='name').squeeze('columns')
    else:
        registry['sgRNA_sequence'] = {}

    amplicon_primers_fn = base_dir / 'targets' / 'amplicon_primers.csv'

    if amplicon_primers_fn.exists():
        registry['amplicon_primers'] = pd.read_csv(amplicon_primers_fn, index_col='name').squeeze('columns')
    else:
        registry['amplicon_primers'] = {}

    extra_sequences_fn = base_dir / 'targets' / 'extra_sequences.csv'
    if extra_sequences_fn.exists():
        registry['extra_sequence'] = pd.read_csv(extra_sequences_fn, index_col='name').squeeze('columns')
    else:
        registry['extra_sequence'] = {}

    donors_fn = base_dir / 'targets' / 'donors.csv'

    if donors_fn.exists():
        donors = pd.read_csv(donors_fn, index_col='name')
        registry['donor_sequence'] = donors['donor_sequence']
        registry['donor_type'] = donors['donor_type']
    else:
        registry['donor_sequence'] = {}
        registry['donor_type'] = {}

    registry['pegRNAs'] = load_pegRNAs(base_dir)

    return registry

def build_target_infos_from_csv(base_dir, offtargets=False, defer_HA_identification=False):
    base_dir = Path(base_dir)
    csv_fn = base_dir / 'targets' / 'targets.csv'

    indices = target_info.locate_supplemental_indices(base_dir)

    targets_df = pd.read_csv(csv_fn, comment='#', index_col='name').replace({np.nan: None})

    registry = build_component_registry(base_dir)

    def lookup(row, column_to_lookup, registry_column, validate_sequence=True, multiple_lookups=False):
        value_to_lookup = row.get(column_to_lookup)
        if value_to_lookup is None:
            return None

        if multiple_lookups:
            values_to_lookup = value_to_lookup.split(';')
        else:
            values_to_lookup = [value_to_lookup]

        registered_values = registry[registry_column]
        valid_chars = set('TCAGN;')

        looked_up = []
        for value_to_lookup in values_to_lookup:
            if value_to_lookup in registered_values:
                value_name = value_to_lookup
                seq = registered_values[value_to_lookup]
                possible_error_message = f'invalid char in {row.name} {column_to_lookup} registry entry {value_to_lookup}\n{seq}'
            else:
                value_name = None
                seq = value_to_lookup
                possible_error_message = f'Error: {row.name} value for {column_to_lookup} ({seq}) is not a registered name but also doesn\'t look like a valid sequence.\nRegistered names: {registered_values}'

            if seq is not None and validate_sequence:
                seq = seq.upper()
                invalid_chars = set(seq) - valid_chars
                if invalid_chars:
                    print(possible_error_message)
                    print(f'Valid sequence characters are {valid_chars}; {seq} contains {invalid_chars}')
                    sys.exit(1)

            looked_up.append((value_name, seq))

        if not multiple_lookups:
            looked_up = looked_up[0]
            
        return looked_up

    for target_name, row in targets_df.iterrows():
        info = {
            'name': target_name,
            'donor_sequence': lookup(row, 'donor_sequence', 'donor_sequence'),
            'sgRNA_sequence': lookup(row, 'sgRNA_sequence', 'sgRNA_sequence', multiple_lookups=True),
            'pegRNAs': lookup(row, 'pegRNAs', 'pegRNAs', multiple_lookups=True, validate_sequence=False),
            'extra_sequences': lookup(row, 'extra_sequences', 'extra_sequence', multiple_lookups=True),
            'amplicon_primers': lookup(row, 'amplicon_primers', 'amplicon_primers'),
            'nonhomologous_donor_sequence': lookup(row, 'nonhomologous_donor_sequence', 'donor_sequence'),
            'donor_type': lookup(row, 'donor_sequence', 'donor_type', validate_sequence=False),
            'genome': row['genome'],
        }

        if 'effector' in row:
            info['effector'] = row['effector']

        if info['sgRNA_sequence'] is None:
            info['sgRNA_sequence'] = []

        if info['pegRNAs'] is not None:
            for pegRNA_name, pegRNA_components in info['pegRNAs']:
                if pegRNA_name is None:
                    # Because of how lookup works, pegRNA_components will hold value of 
                    # name that wasn't found.
                    raise ValueError(f'{pegRNA_components} not found')

                info['sgRNA_sequence'].append((pegRNA_name, pegRNA_components['protospacer']))

            pegRNA_effectors = {components['effector'] for name, components in info['pegRNAs']}
            if len(pegRNA_effectors) > 1:
                raise ValueError('pegRNAs with different effectors', info['pegRNAs'])
            elif len(pegRNA_effectors) == 1:
                info['effector'] = list(pegRNA_effectors)[0]

        if row.get('extra_genbanks') is not None:
            info['extra_genbanks'] = row['extra_genbanks'].split(';')

        logging.info(f'Building {target_name}...')

        build_target_info(base_dir, info, indices,
                          offtargets=offtargets,
                          defer_HA_identification=defer_HA_identification,
                         )

def build_indices(base_dir, name, num_threads=1, **STAR_index_kwargs):
    base_dir = Path(base_dir)

    logging.info(f'Building indices for {name}')
    fasta_dir = base_dir / 'indices' / name / 'fasta'

    fasta_fns = genomes.get_all_fasta_file_names(fasta_dir)
    if len(fasta_fns) == 0:
        raise ValueError(f'No fasta files found in {fasta_dir}')
    elif len(fasta_fns) > 1:
        raise ValueError(f'Can only build minimap2 index from a single fasta file')

    logging.info('Indexing fastas...')
    genomes.make_fais(fasta_dir)

    fasta_fn = fasta_fns[0]

    logging.info('Building STAR index...')
    STAR_dir = base_dir / 'indices' / name / 'STAR'
    STAR_dir.mkdir(exist_ok=True)
    mapping_tools.build_STAR_index([fasta_fn], STAR_dir,
                                   num_threads=num_threads,
                                   RAM_limit=int(4e10),
                                   **STAR_index_kwargs,
                                  )

    logging.info('Building minimap2 index...')
    minimap2_dir = base_dir / 'indices' / name / 'minimap2'
    minimap2_dir.mkdir(exist_ok=True)
    minimap2_index_fn = minimap2_dir / f'{name}.mmi'
    mapping_tools.build_minimap2_index(fasta_fn, minimap2_index_fn)

def download_genome_and_build_indices(base_dir, genome_name, num_threads=8):
    urls = {
        'hg38': 'http://hgdownload.cse.ucsc.edu/goldenPath/hg38/bigZips/hg38.fa.gz',
        'hg19': 'http://hgdownload.cse.ucsc.edu/goldenPath/hg19/bigZips/hg19.fa.gz',
        'bosTau7': 'http://hgdownload.cse.ucsc.edu/goldenPath/bosTau7/bigZips/bosTau7.fa.gz',
        'mm10': 'ftp://ftp.ensembl.org/pub/release-98/fasta/mus_musculus/dna/Mus_musculus.GRCm38.dna.toplevel.fa.gz',
        'e_coli': 'ftp://ftp.ensemblgenomes.org/pub/bacteria/release-44/fasta/bacteria_0_collection/escherichia_coli_str_k_12_substr_mg1655/dna/Escherichia_coli_str_k_12_substr_mg1655.ASM584v2.dna.chromosome.Chromosome.fa.gz',
    }

    if genome_name not in urls:
        print(f'No URL known for {genome_name}.')
        print('Valid options are:')
        for gn in sorted(urls):
            print(f'\t- {gn}')
        sys.exit(1)

    base_dir = Path(base_dir)
    genome_dir = base_dir / 'indices' / genome_name
    fasta_dir = genome_dir / 'fasta'

    logging.info(f'Downloading {genome_name}...')

    wget_command = [
        'wget',
        '--quiet',
        urls[genome_name],
        '-P', str(fasta_dir),
    ]
    subprocess.run(wget_command, check=True)

    logging.info('Uncompressing...')

    file_name = Path(urlparse(urls[genome_name]).path).name

    gunzip_command = [
        'gunzip', '--force',  str(fasta_dir / file_name),
    ]
    subprocess.run(gunzip_command, check=True)

    if genome_name == 'e_coli':
        STAR_index_kwargs = {
            'wonky_param': 4,
        }
    else:
        STAR_index_kwargs = {}

    build_indices(base_dir, genome_name, num_threads=num_threads, **STAR_index_kwargs)

def build_manual_target(base_dir, target_name):
    target_dir = base_dir / 'targets' / target_name

    gb_fns = sorted(target_dir.glob('*.gb'))

    if len(gb_fns) != 1:
        raise ValueError

    gb_fn = gb_fns[0]

    records = list(Bio.SeqIO.parse(str(gb_fn), 'genbank'))

    if len(records) != 1:
        raise ValueError

    record = records[0]

    manifest = {
        'sources': [gb_fn.stem],
        'target': record.id,
    }

    manifest_fn = target_dir / 'manifest.yaml'

    with manifest_fn.open('w') as fh:
        fh.write(yaml.dump(manifest, default_flow_style=False))

    ti = target_info.TargetInfo(base_dir, target_name)
    ti.make_references()    
    ti.identify_degenerate_indels()