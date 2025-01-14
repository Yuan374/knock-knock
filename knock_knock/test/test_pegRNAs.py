import textwrap

import Bio.Align
import numpy as np
import pytest
import yaml

from pathlib import Path

import knock_knock.pegRNAs
import knock_knock.target_info

base_dir = Path(__file__).parent

def populate_source_dir(source_dir):
    if source_dir is None:
        source_dir = base_dir
    return Path(source_dir)

def test_intended_insertion_inference():
    for pegRNA_name, (start, end, strand) in [
        ('pPC1044', (132, 152, '-')),
    ]:
        ti = knock_knock.target_info.TargetInfo(base_dir,
                                                'pPC1000',
                                                sgRNAs=pegRNA_name,
                                               )
        assert len(ti.pegRNA_programmed_insertion_features) == 1
        inferred_insertion = ti.pegRNA_programmed_insertion_features[0]
        assert (inferred_insertion.start == start)
        assert (inferred_insertion.end == end)
        assert (inferred_insertion.strand == strand)

@pytest.mark.skip
def test_intended_deletion_inference():
    for pegRNA, expected_as_string in [
        ('HEK3_4g_del1-5',  'D:{676|677},5'),
        ('HEK3_4g_del1-10', 'D:677,10'),
        ('HEK3_4g_del1-15', 'D:677,15'),
        ('HEK3_4g_del1-25', 'D:{676|677},25'),
        ('HEK3_4g_del1-30', 'D:{677|678},30'),
        ('HEK3_4g_del1-80', 'D:677,80'),
    ]:
        ti = knock_knock.target_info.TargetInfo(base_dir,
                                                'PMID31634902_HEK3',
                                                sgRNAs=[pegRNA],
                                               )

        expected = knock_knock.target_info.degenerate_indel_from_string(expected_as_string)
        assert (ti.pegRNA_programmed_deletion == expected)

@pytest.mark.skip
def test_twin_prime_intended_deletion_inference():
    for pegRNA_pair, expected_as_string, is_prime_del in [
        (('sample01_pegRNA1', 'sample01_pegRNA2'), 'D:{3203|3204},50', True),
        (('sample11_pegRNA1', 'sample11_pegRNA2'), 'D:{3223|3224|3225|3226},30', False),
        (('sample12_pegRNA1', 'sample12_pegRNA2'), 'D:3204,30', False),
        (('sample13_pegRNA1', 'sample13_pegRNA2'), 'D:{3215|3216},28', False),
        (('sample14_pegRNA1', 'sample14_pegRNA2'), 'D:3212,34', False),
        (('220224_sample07_pegRNA1', '220224_sample07_pegRNA2'), None, False),
    ]:

        # Test in both forward and reverse orientations:
        for sequencing_start_feature_name in ['forward_primer', 'gDNA_reverse_primer']:
            ti = knock_knock.target_info.TargetInfo(base_dir,
                                                    'pPC1655',
                                                    sgRNAs=pegRNA_pair,
                                                    sequencing_start_feature_name=sequencing_start_feature_name,
                                                   )

            expected = knock_knock.target_info.degenerate_indel_from_string(expected_as_string)
            assert (ti.pegRNA_programmed_deletion == expected)
            assert (ti.is_prime_del == is_prime_del)

def test_pegRNA_PBS_and_RTT_inference():
    ti = knock_knock.target_info.TargetInfo(base_dir, 'PAH_E4-2_45_EvoPreQ1-4_43_EvoPreQ1')

    feature = ti.features['PAH_E4', 'PAH_E4.2_45_EvoPreQ1_PBS']
    assert (feature.start, feature.end, feature.strand) == (612, 624, '-')

    feature = ti.features['PAH_E4', 'PAH_E4.4_43_EvoPreQ1_PBS'] 
    assert (feature.start, feature.end, feature.strand) == (536, 547, '+')

    # EMX1 has repetitive sequence at the nick that leads to a spurious
    # 7-mer match of nick sequence to the wrong part of the pegRNA and 
    # could cause incorrect PBS inference.

    ti = knock_knock.target_info.TargetInfo(base_dir, 'EMX1', sgRNAs='EMX1_3b')

    target_PBS = ti.features['EMX1', 'EMX1_3b_PBS']
    assert (target_PBS.start, target_PBS.end, target_PBS.strand) == (653, 667, '+')
    pegRNA_PBS = ti.features['EMX1_3b', 'PBS']
    assert (pegRNA_PBS.start, pegRNA_PBS.end, pegRNA_PBS.strand) == (109, 123, '-')
    pegRNA_RTT = ti.features['EMX1_3b', 'RTT']
    assert (pegRNA_RTT.start, pegRNA_RTT.end, pegRNA_RTT.strand) == (96, 108, '-')

def test_twin_prime_overlap_inference():
    ti = knock_knock.target_info.TargetInfo(base_dir, 'HEK3_attB_A30_B30')

    A_feature = ti.features['HEK3_attB_A_30', 'overlap']
    B_feature = ti.features['HEK3_attB_B_30', 'overlap']

    assert (A_feature.start, A_feature.end) == (96, 117)
    assert (B_feature.start, B_feature.end) == (96, 117)
    assert {A_feature.strand, B_feature.strand} == {'+', '-'}

    ti = knock_knock.target_info.TargetInfo(base_dir, 'PAH_E4-2_45_EvoPreQ1-4_43_EvoPreQ1')

    feature_2_45 = ti.features['PAH_E4.2_45_EvoPreQ1', 'overlap']
    feature_4_43 = ti.features['PAH_E4.4_43_EvoPreQ1', 'overlap']

    assert (feature_2_45.start, feature_2_45.end) == (97, 120)
    assert (feature_4_43.start, feature_4_43.end) == (97, 120)
    assert {feature_2_45.strand, feature_4_43.strand} == {'+', '-'}

def load_RTT_alignments(source_dir=None):
    source_dir = populate_source_dir(source_dir)
    yaml_fn = source_dir / 'RTT_alignments.yaml'
    RTT_alignments = yaml.safe_load(yaml_fn.read_text())
    return RTT_alignments

def test_alignment_of_RTT_to_target(flap_sequence, downstream_genomic_sequence, expected_coordinates):
    aligner = knock_knock.pegRNAs.get_RTT_aligner()
    best_alignment = aligner.align(flap_sequence, downstream_genomic_sequence)[0]
    coordinates = list(map(list, best_alignment.coordinates))

    expected_alignment = Bio.Align.Alignment([flap_sequence, downstream_genomic_sequence], np.array(expected_coordinates))
    diagnostic_message = (f'''Reported alignment:
{knock_knock.pegRNAs.trim_excess_target_from_alignment(best_alignment)}
Expected alignment:
{knock_knock.pegRNAs.trim_excess_target_from_alignment(expected_alignment)}
    ''')

    assert coordinates == expected_coordinates, diagnostic_message