import array
import warnings
from pathlib import Path
from collections import defaultdict

import yaml
import pysam
import Bio.SeqIO
import Bio.SeqUtils
import numpy as np
import pandas as pd

from Bio import BiopythonWarning
from Bio.SeqFeature import SeqFeature, FeatureLocation
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import generic_dna

from hits import fasta, fastq, gff, utilities, mapping_tools, interval, sam, sw, genomes

memoized_property = utilities.memoized_property

class Effector():
    def __init__(self, name, PAM_pattern, PAM_side, cut_after_offset):
        self.name = name
        self.PAM_pattern = PAM_pattern
        self.PAM_side = PAM_side
        # cut_after_offset is relative to the 5'-most nt of the PAM
        self.cut_after_offset = cut_after_offset

effectors = {
    'SpCas9': Effector('SpCas9', 'NGG', 3, -4),
    'SaCas9': Effector('SaCas9', 'NNGRRT', 3, -4),
    'Cpf1': Effector('Cpf1', 'TTTN', 5, (22, 26)),
}

class TargetInfo():
    def __init__(self, base_dir, name,
                 donor=None,
                 nonhomologous_donor=None,
                 sgRNA=None,
                 primer_names=None,
                 supplemental_headers=None,
                 ):
        self.name = name
        self.dir = Path(base_dir) / 'targets' / name

        manifest_fn = self.dir / 'manifest.yaml'
        manifest = yaml.safe_load(manifest_fn.open())
        self.target = manifest['target']
        self.sources = manifest['sources']

        self.sgRNA = sgRNA
        self.donor = donor
        self.nonhomologous_donor = nonhomologous_donor
        self.donor_specific = manifest.get('donor_specific', 'GFP11') 
        self.supplemental_headers = supplemental_headers

        self.default_HAs = manifest.get('default_HAs')

        self.fns = {
            'ref_fasta': self.dir / 'refs.fasta',
            'ref_gff': self.dir / 'refs.gff',

            'protospacer_fasta': self.dir / 'protospacers.fasta',
            'protospacer_STAR_prefix_template': self.dir / 'protospacers_{}.',
            'protospacer_bam_template': self.dir / 'protospacers_{}.bam',

            'bowtie2_index': self.dir / 'refs',
            'STAR_index_dir': self.dir / 'STAR_index',

            'degenerate_insertions': self.dir / 'degenerate_insertions.txt',
            'degenerate_deletions': self.dir / 'degenerate_deletions.txt',
        }
        
        self.primer_names = primer_names

    @memoized_property
    def header(self):
        return sam.header_from_fasta(self.fns['ref_fasta'])

    @memoized_property
    def sgRNAs(self):
        if self.sgRNA is None:
            sgRNAs = sorted(n for t, n in self.all_sgRNA_features)
        elif isinstance(self.sgRNA, list):
            sgRNAs = self.sgRNA
        else:
            sgRNAs = [self.sgRNA]

        return sgRNAs

    def make_references(self):
        ''' Generate fasta and gff files from genbank inputs. '''
        gbs = [self.dir / (source + '.gb') for source in self.sources]
        
        fasta_records = []
        all_gff_features = []
        
        for gb in gbs:
            fasta_record, gff_features = parse_benchling_genbank(gb)
            fasta_records.append(fasta_record)
            all_gff_features.extend(gff_features)
            
        with self.fns['ref_fasta'].open('w') as fasta_fh:
            for record in fasta_records:
                fasta_fh.write(str(record))
                
        pysam.faidx(str(self.fns['ref_fasta']))
                
        with self.fns['ref_gff'].open('w') as gff_fh:
            gff_fh.write('##gff-version 3\n')
            for feature in sorted(all_gff_features):
                # Note: all sgRNAs should have feature field format 'sgRNA_{effector}'
                if feature.feature.startswith('sgRNA'):
                    try:
                        _, effector = feature.feature.split('_')
                    except:
                        print(self.name, feature)
                        raise
                    feature.feature = 'sgRNA'
                    feature.attribute['effector'] = effector

                gff_fh.write(str(feature) + '\n')

    def make_protospacer_fastas(self):
        with open(self.fns['protospacer_fasta'], 'w') as fh:
            for (target, sgRNA_name), feature in sorted(self.all_sgRNA_features.items()):
                seq = self.target_sequence[feature.start:feature.end + 1]
                record = fasta.Read(sgRNA_name, seq)
                fh.write(str(record))

    def map_protospacers(self):
        indices = {
            'hg19': '/nvme/indices/refdata-cellranger-hg19-1.2.0/star',
            'GRCh38': '/nvme/indices/refdata-cellranger-GRCh38-1.2.0/star/',
        }

        for index_name, index_dir in indices.items():
            output_prefix = str(self.fns['protospacer_STAR_prefix_template']).format(index_name)
            bam_fn = str(self.fns['protospacer_bam_template']).format(index_name)
            mapping_tools.map_STAR(self.fns['protospacer_fasta'], index_dir, output_prefix, mode='guide_alignment', bam_fn=bam_fn)

    def mapped_protospacer_locations(self, sgRNA_name, index_name):
        bam_fn = str(self.fns['protospacer_bam_template']).format(index_name)
        locations = set()

        with pysam.AlignmentFile(bam_fn) as bam_fh:
            for al in bam_fh:
                if al.query_name == sgRNA_name:
                    locations.add((al.reference_name, al.reference_start))

        return locations
    
    @memoized_property
    def mapped_protospacer_location(self):
        if len(self.sgRNAs) > 1:
            raise ValueError
        else:
            sgRNA = self.sgRNAs[0]

        index_name = 'GRCh38'
        locations = self.mapped_protospacer_locations(sgRNA, index_name)
        
        if len(locations) > 1:
            raise ValueError(locations)
        else:
            location = locations.pop()

        return location

    def make_bowtie2_index(self):
        mapping_tools.build_bowtie2_index(self.fns['bowtie2_index'], [self.fns['ref_fasta']])
    
    @memoized_property
    def features(self):
        features = {
            (f.seqname, f.attribute['ID']): f
            for f in gff.get_all_features(self.fns['ref_gff'])
            if 'ID' in f.attribute
        }
        return features

    @memoized_property
    def features_to_show(self):
        whitelist = {
            (self.donor, 'GFP'),
            (self.donor, 'GFP11'),
            (self.donor, 'PPX'),
        }

        for side in [5, 3]:
            primer = (self.target, self.primers_by_side_of_target[side].attribute['ID'])
            target_HA = (self.target, self.homology_arms[side]['target'].attribute['ID'])

            whitelist.add(primer)
            whitelist.add(target_HA)

            if self.has_shared_homology_arms:
                donor_HA = (self.donor, self.homology_arms[side]['donor'].attribute['ID'])
                whitelist.add(donor_HA)


        return whitelist

    @memoized_property
    def sequencing_start(self):
        return self.features.get((self.target, 'sequencing_start'))

    @memoized_property
    def reference_sequences(self):
        if self.fns['ref_fasta'].exists():
            seqs = fasta.to_dict(self.fns['ref_fasta'])
        else:
            seqs = {}

        return seqs

    @memoized_property
    def target_sequence(self):
        return self.reference_sequences[self.target]
    
    @memoized_property
    def target_sequence_bytes(self):
        return self.target_sequence.encode()
    
    @memoized_property
    def donor_sequence_bytes(self):
        if self.donor_sequence is None:
            return None
        else:
            return self.donor_sequence.encode()

    @memoized_property
    def seed_and_extender(self):
        def fake_extender(*args):
            return [] 

        extenders = {
            'target': sw.SeedAndExtender(self.target_sequence_bytes, 20, self.header, self.target).seed_and_extend,
        }
        if self.donor_sequence_bytes is not None:
            extenders['donor'] = sw.SeedAndExtender(self.donor_sequence_bytes, 20, self.header, self.donor).seed_and_extend
        else:
            # If there isn't a donor, always return no alignments.
            extenders['donor'] = fake_extender

        return extenders

    @memoized_property
    def donor_sequence(self):
        if self.donor is None:
            return None
        else:
            return self.reference_sequences[self.donor]

    @memoized_property
    def sgRNA_features(self):
        return {name: self.features[self.target, name] for name in self.sgRNAs}
    
    @memoized_property
    def sgRNA_feature(self):
        features = list(self.sgRNA_features.values())
        if len(features) > 1:
            raise ValueError(len(features))
        return features[0]

    @memoized_property
    def all_sgRNA_features(self):
        return {name: feature for name, feature in self.features.items() if feature.feature == 'sgRNA'}
    
    @memoized_property
    def guide_slices(self):
        guide_slices = {name: slice(sgRNA.start, sgRNA.end + 1) for name, sgRNA in self.sgRNA_features.items()}
        return guide_slices
    
    @memoized_property
    def guide_slice(self):
        if len(self.guide_slices) > 1:
            raise ValueError(self.guide_slices)
        else:
            return list(self.guide_slices.values())[0]
    
    @memoized_property
    def PAM_slices(self):
        PAM_slices = {}

        for name, sgRNA in self.sgRNA_features.items():
            effector = effectors[sgRNA.attribute['effector']]

            before_slice = slice(sgRNA.start - len(effector.PAM_pattern), sgRNA.start)
            after_slice = slice(sgRNA.end + 1, sgRNA.end + 1 + len(effector.PAM_pattern))

            if (sgRNA.strand == '+' and effector.PAM_side == 5) or (sgRNA.strand == '-' and effector.PAM_side == 3):
                PAM_slice = before_slice
            else:
                PAM_slice = after_slice

            PAM_slices[name] = PAM_slice

            PAM_seq = self.target_sequence[PAM_slice].upper()
            if sgRNA.strand == '-':
                PAM_seq = utilities.reverse_complement(PAM_seq)
            pattern, *matches = Bio.SeqUtils.nt_search(PAM_seq, effector.PAM_pattern) 
            if 0 not in matches:
                raise ValueError('{}: {} doesn\'t match {} PAM'.format(name, PAM_seq, pattern))

        return PAM_slices
    
    @memoized_property
    def PAM_slice(self):
        if len(self.PAM_slices) > 1:
            raise ValueError(self.PAM_slices)
        else:
            return list(self.PAM_slices.values())[0]

    @memoized_property
    def effector(self):
        return effectors[self.sgRNA_feature.attribute['effector']]

    @memoized_property
    def cut_afters(self):
        cut_afters = {}
        for name, sgRNA in self.sgRNA_features.items():
            effector = effectors[sgRNA.attribute['effector']]
            if isinstance(effector.cut_after_offset, int):
                offsets = [effector.cut_after_offset]
                key_suffixes = ['']
            else:
                offsets = effector.cut_after_offset
                key_suffixes = ['_{}'.format(i) for i in range(len(offsets))]

            for offset, key_suffix in zip(offsets, key_suffixes):
                if sgRNA.strand == '+':
                    PAM_5 = self.PAM_slices[name].start
                    cut_after = PAM_5 + offset
                else:
                    PAM_5 = self.PAM_slices[name].stop - 1
                    cut_after = PAM_5 - offset
                
                cut_afters[name + key_suffix] = cut_after

        return cut_afters
    
    @memoized_property
    def cut_after(self):
        ''' when processing assumes there will be only one cut, use this '''
        return min(self.cut_afters.values())

    def around_cuts(self, each_side):
        intervals = [interval.Interval(cut_after - each_side + 1, cut_after + each_side) for cut_after in self.cut_afters.values()]
        return interval.DisjointIntervals(intervals)

    @memoized_property
    def overlaps_cut(self):
        ''' only really makes sense if only one cut or Cpf1 '''
        cut_after_ps = self.cut_afters.values()
        cut_interval = interval.Interval(min(cut_after_ps), max(cut_after_ps))

        def overlaps_cut(al):
            return bool(sam.reference_interval(al) & cut_interval)

        return overlaps_cut
        
    @memoized_property
    def most_extreme_primer_names(self):
        # Default to the primers farthest to the left and right on the target.
        all_primer_names = {name: feature for (_, name), feature in self.features.items()
                            if name.startswith('forward_primer') or name.startswith('reverse_primer')
                            }
        left_most = min(all_primer_names, key=lambda n: all_primer_names[n].start)
        right_most = max(all_primer_names, key=lambda n: all_primer_names[n].end)

        primer_names = [left_most, right_most]

        return primer_names

    @memoized_property
    def primers(self):
        primer_names = self.primer_names
        if primer_names is None:
            primer_names = self.most_extreme_primer_names

        primers = {name: self.features[self.target, name] for name in primer_names}

        return primers

    @memoized_property
    def primers_by_PAM_side(self):
        primers = {}

        for primer_name, primer in self.primers.items():
            primer = self.features[self.target, primer_name]
            primer_interval = interval.Interval.from_feature(primer)
            
            for side, PAM_side_interval in self.PAM_side_intervals.items():
                if primer_interval & PAM_side_interval:
                    primers[side] = primer

        if len(primers) != 2:
            raise ValueError('not a primer on each side of cut: {}'.format(primers))

        return primers
    
    @memoized_property
    def primers_by_side_of_target(self):
        by_side = {}
        primers = [primer for primer in self.primers.values()]
        by_side[5], by_side[3] = sorted(primers, key=lambda p: p.start)
        return by_side

    @memoized_property
    def target_side_intervals(self):
        ''' intervals of target 5' and 3' of cut '''
        return {
            5: interval.Interval(0, self.cut_after),
            3: interval.Interval(self.cut_after + 1, np.inf)
        }

    @memoized_property
    def PAM_side_intervals(self):
        return {self.target_side_to_PAM_side[side]: intvl for side, intvl in self.target_side_intervals.items()}
    
    @memoized_property
    def read_side_intervals(self):
        return {self.target_side_to_read_side[side]: intvl for side, intvl in self.target_side_intervals.items()}

    @memoized_property
    def target_side_to_PAM_side(self):
        strand = self.sgRNA_feature.strand
        PAM_side = self.effector.PAM_side

        if (strand == '+' and PAM_side == 3) or (strand == '-' and PAM_side == 5):
            target_to_PAM = {5: 'PAM-proximal', 3: 'PAM-distal'}
        elif (strand == '+' and PAM_side == 5) or (strand == '-' and PAM_side == 3):
            target_to_PAM = {3: 'PAM-proximal', 5: 'PAM-distal'}

        return target_to_PAM

    @memoized_property
    def target_side_to_read_side(self):
        if self.sequencing_start is None:
            return None
        
        read_start_interval = interval.Interval.from_feature(self.sequencing_start)
        
        if self.target_side_intervals[5] & read_start_interval:
            target_to_read = {5: 'left', 3: 'right'}
        elif self.target_side_intervals[3] & read_start_interval:
            target_to_read = {5: 'right', 3: 'left'}
        else:
            raise ValueError
            
        return target_to_read

    @memoized_property
    def read_side_to_target_side(self):
        return utilities.reverse_dictionary(self.target_side_to_read_side)

    @memoized_property
    def PAM_side_to_target_side(self):
        return utilities.reverse_dictionary(self.target_side_to_PAM_side)

    @memoized_property
    def read_side_to_PAM_side(self):
        read_to_PAM = {}

        for target_side, read_side in self.target_side_to_read_side.items():
            PAM_side = self.target_side_to_PAM_side[target_side]
            read_to_PAM[read_side] = PAM_side

        return read_to_PAM
    
    @memoized_property
    def PAM_side_to_read_side(self):
        return utilities.reverse_dictionary(self.read_side_to_PAM_side)

    @memoized_property
    def homology_arms(self):
        ''' HAs keyed by:
                name,
                side of cut (PAM-proximal/PAM-distal),
                side of target (5/3),
                expected side of read (left/right),
        '''

        # Load homology arms from gff features.
        HAs = defaultdict(dict)

        ref_name_to_source = {
            self.target: 'target',
            self.donor: 'donor',
        }

        for (ref_name, feature_name), feature in self.features.items():
            source = ref_name_to_source.get(ref_name)
            if source is not None:
                if feature_name.startswith('HA_'):
                    HAs[feature_name][source] = feature
                    
        paired_HAs = {}

        # Confirm that every HA name that exists on both the target and donor has the same
        # sequence on each.
        for name in HAs:
            if 'target' not in HAs[name] or 'donor' not in HAs[name]:
                continue
            
            seqs = {}
            for source in ['target', 'donor']:
                ref_seq = self.reference_sequences[getattr(self, source)]
                HA = HAs[name][source]
                HA_seq = ref_seq[HA.start:HA.end + 1]
                if HA.strand == '-':
                    HA_seq = utilities.reverse_complement(HA_seq)
                    
                seqs[source] = HA_seq
                
            if seqs['target'] != seqs['donor']:
                print(f'warning: {name} not identical sequence on target and donor')

            paired_HAs[name] = HAs[name]

        if len(paired_HAs) != 2:
            # If there is only one set of homology arms to use, just use it.
            if len(HAs) == 2:
                for name in HAs:
                    paired_HAs[name] = HAs[name]
            else:
                # otherwise need a default set to be specified
                if self.default_HAs is None:
                    raise ValueError('need to specify default_HAs if no homologous donor')
                else:
                    for name in self.default_HAs:
                        paired_HAs[name] = HAs[name]

        by_target_side = {}
        by_target_side[5], by_target_side[3] = sorted(paired_HAs, key=lambda n: HAs[n]['target'].start)

        for target_side, name in by_target_side.items():
            PAM_side = self.target_side_to_PAM_side[target_side]

            # If sequencing start isn't annotated, don't populate by read side.
            if self.target_side_to_read_side is not None:
                read_side = self.target_side_to_read_side[target_side]
            else:
                read_side = None

            for key in [target_side, PAM_side, read_side]:
                if key is not None:
                    if key in HAs:
                        raise ValueError(key)
                    HAs[key] = HAs[name]

        return HAs

    @memoized_property
    def has_shared_homology_arms(self):
        has_shared_arms = False

        for name, d in self.homology_arms.items():
            if 'target' in d and 'donor' in d:
                has_shared_arms = True

        return has_shared_arms

    @memoized_property
    def HA_ref_p_to_offset(self):
        ''' register which positions in target and donor sequences correspond
        to the same offset in each homology arm
        '''
        ref_p_to_offset = {}

        for PAM_side in self.homology_arms:
            for name in self.homology_arms[PAM_side]:
                feature = self.homology_arms[PAM_side][name]
            
                if feature.strand == '+':
                    order = range(feature.start, feature.end + 1)
                else:
                    order = range(feature.end, feature.start - 1, -1)
                    
                ref_p_to_offset[name, PAM_side] = {ref_p: offset for offset, ref_p in enumerate(order)}  

        for name in ['target', 'donor']:
            for side in ['left', 'right']:
                ref_p_to_offset[name, side] = ref_p_to_offset[name, self.read_side_to_PAM_side[side]]

        return ref_p_to_offset

    @memoized_property
    def amplicon_interval(self):
        primers = self.primers_by_side_of_target
        return interval.Interval(primers[5].start, primers[3].end)

    @memoized_property
    def amplicon_length(self):
        return len(self.amplicon_interval)

    @memoized_property
    def clean_HDR_length(self):
        if self.has_shared_homology_arms:
            HAs = self.homology_arms
            return self.amplicon_length + HAs[3]['donor'].start - HAs[5]['donor'].end - 1
        else:
            return None

    @memoized_property
    def fingerprints(self):
        fps = {
            self.target: [],
            self.donor: [],
        }

        for name in self.SNP_names:
            fs = {k: self.features[k, name] for k in (self.target, self.donor)}
            ps = {k: (f.strand, f.start) for k, f in fs.items()}
            bs = {k: self.reference_sequences[k][p:p + 1] for k, (strand, p) in ps.items()}
            
            for k in bs:
                if fs[k].strand == '-':
                    bs[k] = utilities.reverse_complement(bs[k])
                
                fps[k].append((ps[k], bs[k]))

        return fps

    @memoized_property
    def SNP_names(self):
        return sorted([name for seq_name, name in self.features if seq_name == self.donor and name.startswith('SNP')])

    @memoized_property
    def donor_SNVs(self):
        SNVs = {'target': {}, 'donor': {}}

        if self.donor is None:
            return SNVs

        for key, seq_name in [('target', self.target), ('donor', self.donor)]:
            seq = self.reference_sequences[seq_name]
            for name in self.SNP_names:
                feature = self.features[seq_name, name]
                strand = feature.strand
                position = feature.start
                b = seq[position:position + 1]
                if strand == '-':
                    b = utilities.reverse_complement(b)

                SNVs[key][name] = {
                    'position': position,
                    'strand': strand,
                    'base': b
                }
            
        return SNVs
    
    @memoized_property
    def simple_donor_SNVs(self):
        ''' {ref_name, position: base identity in donor if on forward strand}}'''
        SNVs = {}

        for SNV_name, donor_SNV_details in self.donor_SNVs['donor'].items():
            target_SNV_details = self.donor_SNVs['target'][SNV_name]

            donor_base = donor_SNV_details['base']
            if donor_SNV_details['strand'] == '-':
                # We want the forward strand base.
                donor_base = utilities.reverse_complement(donor_base)

            SNVs[self.donor, donor_SNV_details['position']] = donor_base

            # Confusing: we want the base that would be read on the forward
            # strand of target if it is actually the donor SNV.
            if target_SNV_details['strand'] != donor_SNV_details['strand']:
                possibly_flipped_donor_base = utilities.reverse_complement(donor_base)
            else:
                possibly_flipped_donor_base = donor_base

            SNVs[self.target, target_SNV_details['position']] = possibly_flipped_donor_base
        
        return SNVs
    
    @memoized_property
    def donor_deletions(self):
        donor_deletions = []
        for (seq_name, name), feature in self.features.items():
            if seq_name == self.target and feature.feature == 'donor_deletion':
                donor_name = name[:-len('_deletion')]
                if donor_name == self.donor:
                    deletion = DegenerateDeletion([feature.start], len(feature))
                    deletion = self.expand_degenerate_indel(deletion)
                    donor_deletions.append(deletion)

        return donor_deletions

    @memoized_property
    def wild_type_locii(self):
        return ''.join([b for _, b in self.fingerprints[self.target]])
    
    @memoized_property
    def donor_locii(self):
        return ''.join([b for _, b in self.fingerprints[self.donor]])

    def identify_degenerate_indels(self):
        degenerate_dels = defaultdict(list)

        possible_starts = range(self.primers_by_side_of_target[5].start, self.primers_by_side_of_target[3].end)
        for starts_at in possible_starts:
            before = self.target_sequence[:starts_at]
            for length in range(1, 200):
                if starts_at + length >= len(self.target_sequence):
                    continue
                after = self.target_sequence[starts_at + length:]
                result = before + after
                deletion = DegenerateDeletion([starts_at], length)
                degenerate_dels[result].append(deletion)

        with self.fns['degenerate_deletions'].open('w') as fh:
            classes = sorted(degenerate_dels.values(), key=len, reverse=True)
            for degenerate_class in classes:
                if len(degenerate_class) > 1:
                    collapsed = DegenerateDeletion.collapse(degenerate_class)
                    fh.write('{0}\n'.format(collapsed))

        degenerate_inss = defaultdict(list)

        mers = {length: list(utilities.mers(length)) for length in range(1, 5)}

        for starts_after in possible_starts:
            before = self.target_sequence[:starts_after + 1]
            after = self.target_sequence[starts_after + 1:]
            for length in mers:
                for mer in mers[length]:
                    result = before + mer + after
                    insertion = DegenerateInsertion([starts_after], [mer])
                    degenerate_inss[result].append(insertion)

        with self.fns['degenerate_insertions'].open('w') as fh:
            classes = sorted(degenerate_inss.values(), key=len, reverse=True)
            for degenerate_class in classes:
                if len(degenerate_class) > 1:
                    collapsed = DegenerateInsertion.collapse(degenerate_class)
                    fh.write('{0}\n'.format(collapsed))

    @memoized_property
    def degenerate_indels(self):
        singleton_to_full = {}

        for line in self.fns['degenerate_deletions'].open():
            deletion = degenerate_indel_from_string(line.strip())

            for singleton in deletion.singletons():
                singleton_to_full[singleton] = deletion
        
        for line in self.fns['degenerate_insertions'].open():
            insertion = degenerate_indel_from_string(line.strip())

            for singleton in insertion.singletons():
                singleton_to_full[singleton] = insertion

        return singleton_to_full

    def expand_degenerate_indel(self, indel):
        return self.degenerate_indels.get(indel, indel)

def degenerate_indel_from_string(details_string):
    kind, rest = details_string.split(':')

    if kind == 'D':
        return DegenerateDeletion.from_string(details_string)
    elif kind == 'I':
        return DegenerateInsertion.from_string(details_string)

class DegenerateDeletion():
    def __init__(self, starts_ats, length):
        self.kind = 'D'
        self.starts_ats = tuple(starts_ats)
        self.num_MH_nts = len(self.starts_ats) - 1
        self.length = length
        self.ends_ats = [s + self.length - 1 for s in self.starts_ats]

    @classmethod
    def from_string(cls, details_string):
        kind, rest = details_string.split(':', 1)
        starts_string, length_string = rest.split(',')

        starts_ats = [int(s) for s in starts_string.strip('{}').split('|')]
        length = int(length_string)

        return DegenerateDeletion(starts_ats, length)

    @classmethod
    def collapse(cls, degenerate_deletions):
        lengths = {d.length for d in degenerate_deletions}
        if len(lengths) > 1:
            for d in degenerate_deletions:
                print(d)
            raise ValueError
        length = lengths.pop()

        starts_ats = set()
        for d in degenerate_deletions:
            starts_ats.update(d.starts_ats)

        starts_ats = sorted(starts_ats)

        return DegenerateDeletion(starts_ats, length)

    def __str__(self):
        starts_string = '|'.join(map(str, self.starts_ats))
        if len(self.starts_ats) > 1:
            starts_string = '{' + starts_string + '}'

        full_string = 'D:{0},{1}'.format(starts_string, self.length)

        return full_string

    def __repr__(self):
        return str(self)
    
    def __eq__(self, other):
        return self.starts_ats == other.starts_ats and self.length == other.length

    def __hash__(self):
        return hash((self.starts_ats, self.length))

    def singletons(self):
        return (DegenerateDeletion([starts_at], self.length) for starts_at in self.starts_ats)
    
class DegenerateInsertion():
    def __init__(self, starts_afters, seqs):
        self.kind = 'I'
        self.starts_afters = tuple(starts_afters)
        self.seqs = tuple(seqs)
        
        lengths = set(len(seq) for seq in self.seqs)
        if len(lengths) > 1:
            raise ValueError
        self.length = lengths.pop()
    
        self.pairs = list(zip(self.starts_afters, self.seqs))

    @classmethod
    def from_string(cls, details_string):
        kind, rest = details_string.split(':', 1)
        starts_string, seqs_string = rest.split(',')
        starts_afters = [int(s) for s in starts_string.strip('{}').split('|')]
        seqs = [seq for seq in seqs_string.strip('{}').split('|')]

        return DegenerateInsertion(starts_afters, seqs)
    
    @classmethod
    def from_pairs(cls, pairs):
        starts_afters, seqs = zip(*pairs)
        return DegenerateInsertion(starts_afters, seqs)

    def __str__(self):
        starts_string = '|'.join(map(str, self.starts_afters))
        seqs_string = '|'.join(self.seqs)

        if len(self.starts_afters) > 1:
            starts_string = '{' + starts_string + '}'
            seqs_string = '{' + seqs_string + '}'

        full_string = 'I:{0},{1}'.format(starts_string, seqs_string)

        return full_string

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        return self.starts_afters == other.starts_afters and self.seqs == other.seqs
    
    def __hash__(self):
        return hash((self.starts_afters, self.seqs))

    def singletons(self):
        return (DegenerateInsertion([starts_after], [seq]) for starts_after, seq in self.pairs)
    
    @classmethod
    def collapse(cls, degenerate_insertions):
        all_pairs = []

        for d in degenerate_insertions:
            all_pairs.extend(d.pairs)

        all_pairs = sorted(all_pairs)

        return DegenerateInsertion.from_pairs(all_pairs)

class SNV():
    def __init__(self, position, basecall, quality):
        self.position = position
        self.basecall = basecall
        self.quality = quality

    @classmethod
    def from_string(cls, details_string):
        basecall = details_string[-1]

        if basecall.islower():
            quality = 0
        else:
            quality = 40

        position = int(details_string[:-1])

        return SNV(position, basecall, quality)

    def __str__(self):
        if self.quality < 30:
            bc = self.basecall.lower()
        else:
            bc = self.basecall

        return '{}{}'.format(self.position, bc)

class SNVs():
    def __init__(self, snvs):
        self.snvs = sorted(snvs, key=lambda snv: snv.position)
    
    def __str__(self):
        return ','.join(str(snv) for snv in self.snvs)

    @classmethod
    def from_string(cls, details_string):
        return SNVs([SNV.from_string(s) for s in details_string.split(',')])

    def __repr__(self):
        return str(self)

    def __len__(self):
        return len(self.snvs)

    def __iter__(self):
        return iter(self.snvs)

    @property
    def positions(self):
        return [snv.position for snv in self.snvs]
    
    @property
    def basecalls(self):
        return [snv.basecall for snv in self.snvs]

    def __lt__(self, other):
        if max(self.positions) != max(other.positions):
            return max(self.positions) < max(other.positions)
        else:
            if len(self) < len(other):
                return True
            elif len(self) == len(other):
                if self.positions != other.positions:
                    return self.positions < other.positions
                else: 
                    return self.basecalls < other.basecalls
            else:
                return False

def parse_benchling_genbank(genbank_fn):
    convert_strand = {
        -1: '-',
        1: '+',
    }

    gb_record = Bio.SeqIO.read(str(genbank_fn), 'genbank')

    fasta_record = fasta.Read(gb_record.name, str(gb_record.seq))

    gff_features = []

    for gb_feature in gb_record.features:
        feature = gff.Feature.from_fields(
            gb_record.id,
            '.',
            gb_feature.type,
            gb_feature.location.start,
            gb_feature.location.end - 1,
            '.',
            convert_strand[gb_feature.location.strand],
            '.',
            '.',
        )
        attribute = {k: v[0] for k, v in gb_feature.qualifiers.items()}

        if 'label' in attribute:
            attribute['ID'] = attribute.pop('label')

        if 'ApEinfo_fwdcolor' in attribute:
            attribute['color'] = attribute.pop('ApEinfo_fwdcolor')

        if 'ApEinfo_revcolor' in attribute:
            attribute.pop('ApEinfo_revcolor')

        feature.attribute = attribute

        gff_features.append(feature)

    return fasta_record, gff_features

def get_all_targets(base_dir):
    targets_dir = Path(base_dir) / 'targets'
    names = (p.name for p in targets_dir.glob('*') if p.is_dir())
    targets = [TargetInfo(base_dir, n) for n in names]
    return targets

def build_target_info(info, target_dir):
    target_dir = Path(target_dir)
    
    name = info['name']
    donor_name = f'{name}_donor'
    
    protospacer = info['sgRNA_sequence'].upper()
    amplicon_primers = info['amplicon_primers'].split(';')
    donor_seq = info['donor_sequence'].upper()
    
    protospacer_dir = target_dir / 'protospacer_alignment'
    protospacer_dir.mkdir(exist_ok=True)
    fastq_fn = protospacer_dir / 'protospacer.fastq'
    STAR_prefix = protospacer_dir / 'protospacer_'
    bam_fn = protospacer_dir / 'protospacer.bam'

    index_location = '/nvme/indices/refdata-cellranger-hg19-1.2.0/star'

    target_gb_fn = target_dir / f'{name}.gb'
    donor_gb_fn = target_dir / f'{donor_name}.gb'

    # Make a fastq file with a single read containing the protospacer sequence.
    
    with fastq_fn.open('w') as fh:
        quals = fastq.encode_sanger([40]*len(protospacer))
        read = fastq.Read('protospacer', protospacer, quals)
        fh.write(str(read))
        
    # Align the protospacer to the reference genome.
    mapping_tools.map_STAR(fastq_fn, index_location, STAR_prefix, mode='guide_alignment', bam_fn=bam_fn)

    with pysam.AlignmentFile(bam_fn) as bam_fh:
        perfect_als = [al for al in bam_fh if not al.is_unmapped and sam.total_edit_distance(al) == 0]
    
    region_fetcher = genomes.build_region_fetcher('/nvme/indices/refdata-cellranger-hg19-1.2.0/fasta')
    
    def generate_records(al):
        full_window_around = 5000

        full_around = region_fetcher(al.reference_name, al.reference_start - full_window_around, al.reference_end + full_window_around).upper()

        if sam.get_strand(al) == '+':
            ps_seq = protospacer
            ps_strand = 1
            PAM_offset = len(protospacer)
            PAM_transform = utilities.identity
        else:
            ps_seq = utilities.reverse_complement(protospacer)
            ps_strand = -1
            PAM_offset = -3
            PAM_transform = utilities.reverse_complement

        ps_start = full_around.index(ps_seq)
        
        PAM = PAM_transform(full_around[ps_start + PAM_offset:ps_start + PAM_offset + 3])
        pattern, *matches = Bio.SeqUtils.nt_search(PAM, 'NGG')
        if 0 not in matches:
            print('bad PAM')
            return None
        
        if amplicon_primers[0] in full_around:
            final_fwd_primer = amplicon_primers[0]
            final_rev_primer = utilities.reverse_complement(amplicon_primers[1])
            if final_rev_primer not in full_around:
                return None
        else:
            final_fwd_primer = amplicon_primers[1]
            final_rev_primer = utilities.reverse_complement(amplicon_primers[0])

            if final_fwd_primer not in full_around or final_rev_primer not in full_around:
                return None

        fwd_start = full_around.index(final_fwd_primer)
        rev_start = full_around.index(final_rev_primer)
        
        if fwd_start >= rev_start:
            # Non-physical.
            return None
        
        final_window_around = 500    

        offset = fwd_start - final_window_around

        final_start = fwd_start - final_window_around
        final_end = rev_start + len(final_rev_primer) + final_window_around

        target_seq = full_around[final_start:final_end]

        # Identify the homology arms.

        donor_prefix = donor_seq[:20]
        
        if donor_prefix in target_seq:
            possibly_flipped_donor_seq = donor_seq
        elif utilities.reverse_complement(donor_prefix) in target_seq:
            possibly_flipped_donor_seq = utilities.reverse_complement(donor_seq)
            donor_prefix = possibly_flipped_donor_seq[:20]
        else:
            print('no donor prefix')
            return None

        donor_suffix = possibly_flipped_donor_seq[-20:]

        try:
            donor_start = target_seq.index(donor_prefix)
            donor_end = target_seq.index(donor_suffix) + len(donor_suffix)
        except ValueError:
            print('no donor suffix')
            return None

        relevant_target_seq = target_seq[donor_start:donor_end]

        total_HA_length = donor_end - donor_start

        mismatches_before_deletion = np.cumsum([t != d for t, d in zip(relevant_target_seq, possibly_flipped_donor_seq)])

        flipped_target = relevant_target_seq[::-1]
        flipped_donor = possibly_flipped_donor_seq[::-1]
        mismatches_after_deletion = np.cumsum([0] + [t != d for t, d in zip(flipped_target, flipped_donor)][:-1])[::-1]

        total_mismatches = mismatches_before_deletion + mismatches_after_deletion

        last_index_in_HA_1 = int(np.argmin(total_mismatches))

        lengths = {}
        lengths['HA_1'] = last_index_in_HA_1 + 1
        lengths['HA_2'] = total_HA_length - lengths['HA_1']
        lengths['donor_specific'] = len(donor_seq) - total_HA_length
        
        # Support for donors that have fixed sequences outside of their homology arms added by PCR.
        if info.get('donor_primers', '') != '':
            donor_primers = info['donor_primers'].split(';')
            
            suffix_nts = 15

            forward_suffix = donor_primers[0][-suffix_nts:]
            
            if forward_suffix in possibly_flipped_donor_seq:
                donor_fwd = donor_primers[0]
                donor_rev = utilities.reverse_complement(donor_primers[1])

            else:
                forward_suffix = donor_primers[1][-suffix_nts:]

                if forward_suffix in possibly_flipped_donor_seq:
                    donor_fwd = donor_primers[1]
                    donor_rev = utilities.reverse_complement(donor_primers[0])

                else:
                    print('no fwd suffix')
                    return None

            forward_suffix_start = possibly_flipped_donor_seq.index(forward_suffix)
            extra_nts_at_start = len(donor_fwd[:-suffix_nts]) - forward_suffix_start

            reverse_suffix = donor_rev[:suffix_nts]

            try:
                reverse_suffix_start = possibly_flipped_donor_seq.index(reverse_suffix)
            except ValueError:
                print('no rev suffix')
                return None

            possibly_flipped_donor_seq = donor_fwd[:-suffix_nts] + possibly_flipped_donor_seq[forward_suffix_start:reverse_suffix_start] + donor_rev
        else:
            extra_nts_at_start = 0
        
        starts = {
            'HA_1': extra_nts_at_start + 0,
            'donor_specific': extra_nts_at_start + lengths['HA_1'],
            'HA_2': extra_nts_at_start + len(donor_seq) - lengths['HA_2'], # note: donor_seq is still original
        }
        
        colors = {
            'HA_1': '#c7b0e3',
            'HA_2': '#85dae9',
            'forward_primer': '#ff9ccd',
            'reverse_primer': '#9eafd2',
            'sgRNA': '#c6c9d1',
            'donor_specific': '#b1ff67',
        }
        
        donor_features = [
            SeqFeature(location=FeatureLocation(starts['HA_1'], starts['HA_1'] + lengths['HA_1'], strand=1),
                       id='HA_1',
                       type='misc_feature',
                       qualifiers={'label': 'HA_1',
                                   'ApEinfo_fwdcolor': colors['HA_1'],
                                  },
                      ),
            SeqFeature(location=FeatureLocation(starts['donor_specific'], starts['donor_specific'] + lengths['donor_specific'], strand=1),
                       id='donor_specific',
                       type='misc_feature',
                       qualifiers={'label': 'donor_specific',
                                   'ApEinfo_fwdcolor': colors['donor_specific'],
                                  },
                      ),
            SeqFeature(location=FeatureLocation(starts['HA_2'], starts['HA_2'] + lengths['HA_2'], strand=1),
                       id='HA_2',
                       type='misc_feature',
                       qualifiers={'label': 'HA_2',
                                   'ApEinfo_fwdcolor': colors['HA_2'],
                                  },
                      ),
        ]

        donor_record = Bio.SeqRecord.SeqRecord(Seq(possibly_flipped_donor_seq, generic_dna), id=donor_name, name=donor_name, features=donor_features)

        target_features = [
            SeqFeature(location=FeatureLocation(fwd_start - offset, fwd_start - offset + len(final_fwd_primer), strand=1),
                       id='forward_primer',
                       type='misc_feature',
                       qualifiers={'label': 'forward_primer',
                                   'ApEinfo_fwdcolor': colors['forward_primer'],
                                  },
                      ),
            SeqFeature(location=FeatureLocation(rev_start - offset, rev_start - offset + len(final_rev_primer), strand=-1),
                       id='reverse_primer',
                       type='misc_feature',
                       qualifiers={'label': 'reverse_primer',
                                   'ApEinfo_fwdcolor': colors['reverse_primer'],
                                  },
                      ),
            SeqFeature(location=FeatureLocation(ps_start - offset, ps_start - offset + len(protospacer), strand=ps_strand),
                       id='sgRNA',
                       type='sgRNA_SpCas9',
                       qualifiers={'label': 'sgRNA',
                                   'ApEinfo_fwdcolor': colors['sgRNA'],
                                  },
                      ),
            SeqFeature(location=FeatureLocation(donor_start, donor_start + lengths['HA_1'], strand=1),
                       id='HA_1',
                       type='misc_feature',
                       qualifiers={'label': 'HA_1',
                                   'ApEinfo_fwdcolor': colors['HA_1'],
                                  },
                      ),
            SeqFeature(location=FeatureLocation(donor_end - lengths['HA_2'], donor_end, strand=1),
                       id='HA_2',
                       type='misc_feature',
                       qualifiers={'label': 'HA_2',
                                   'ApEinfo_fwdcolor': colors['HA_2'],
                                  },
                      ),
        ]

        target_record = SeqRecord(Seq(target_seq, generic_dna), id=name, name=name, features=target_features)
        
        return target_record, donor_record
    
    records = []
    
    for al in perfect_als:
        record_pair = generate_records(al)
        if record_pair is not None:
            records.append(record_pair)
    
    if len(records) != 1:
        raise ValueError(name, records)
    else:
        target_record, donor_record = records[0]

    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=BiopythonWarning)
        Bio.SeqIO.write(donor_record, donor_gb_fn, 'genbank')
        Bio.SeqIO.write(target_record, target_gb_fn, 'genbank')

    manifest_fn = target_dir / 'manifest.yaml'
    manifest = {
        'sources': [name, donor_name],
        'target': name,
        'donor_specific': 'donor_specific',
    }
    manifest_fn.write_text(yaml.dump(manifest, default_flow_style=False))

def build_target_infos_from_csv(base_dir):
    base_dir = Path(base_dir)
    csv_fn = base_dir / 'targets' / 'targets.csv'

    df = pd.read_csv(csv_fn)

    for _, row in df.iterrows():
        name = row['name']
        print(f'Building {name}...')
        target_dir = base_dir / 'targets' / name
        target_dir.mkdir(parents=True, exist_ok=True)
        build_target_info(row, target_dir)

        ti = TargetInfo(base_dir, name)
        ti.make_references()
        ti.identify_degenerate_indels()