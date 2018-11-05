import copy
import io
import itertools
from collections import defaultdict

import matplotlib
matplotlib.use('Agg', warn=False)

import PIL
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker
import ipywidgets
import pandas as pd

from sequencing import utilities, interval, sam

from . import experiment as experiment_module
from . import target_info as target_info_module
from . import layout as layout_module

def get_mismatch_info(alignment, target_info):
    mismatches = []

    triples = []
    if target_info.reference_sequences.get(alignment.reference_name) is None:
        for read_p, ref_p, ref_b in alignment.get_aligned_pairs(with_seq=True):
            if read_p != None and ref_p != None:
                read_b = alignment.query_sequence[read_p]
                triples.append((read_p, read_b, ref_b))

    else:
        reference = target_info.reference_sequences[alignment.reference_name]
        for read_p, ref_p in alignment.get_aligned_pairs():
            if read_p != None and ref_p != None:
                read_b = alignment.query_sequence[read_p]
                ref_b = reference[ref_p]
                
                triples.append((read_p, read_b, ref_b))

    for read_p, read_b, ref_b in triples:
        if read_b != ref_b:
            true_read_p = sam.true_query_position(read_p, alignment)
            q = alignment.query_qualities[read_p]

            if alignment.is_reverse:
                read_b = utilities.reverse_complement(read_b)
                ref_b = utilities.reverse_complement(ref_b)

            mismatches.append((true_read_p, read_b, ref_p, ref_b, q))

    return mismatches

def get_indel_info(alignment):
    indels = []
    for i, (kind, length) in enumerate(alignment.cigar):
        if kind == sam.BAM_CDEL:
            nucs_before = sam.total_read_nucs(alignment.cigar[:i])
            centered_at = np.mean([sam.true_query_position(p, alignment) for p in [nucs_before - 1, nucs_before]])
            indels.append(('deletion', (centered_at, length)))

        elif kind == sam.BAM_CINS:
            first_edge = sam.total_read_nucs(alignment.cigar[:i])
            second_edge = first_edge + length
            starts_at, ends_at = sorted(sam.true_query_position(p, alignment) for p in [first_edge, second_edge])
            indels.append(('insertion', (starts_at, ends_at)))
            
    return indels

class ReadDiagram():
    def __init__(self, alignments, target_info,
                 ref_centric=False,
                 parsimonious=False,
                 zoom_in=None,
                 size_multiple=1,
                 paired_end_read_length=None,
                 draw_qualities=False,
                 draw_mismatches=True,
                 draw_polyA=False,
                 draw_sequence=False,
                 max_qual=41,
                 process_mappings=None,
                 detect_orientation=False,
                 label_layout=False,
                 highlight_SNPs=False,
                 highlight_around_cut=False,
                 reverse_complement=None,
                 label_left=False,
                 flip_donor=False,
                 flip_target=False,
                 ax=None,
                 **kwargs):
        self.parsimonious = parsimonious

        self.alignments = copy.deepcopy(alignments)
        if self.parsimonious:
            self.alignments = interval.make_parsimonious(self.alignments)
            
        self.target_info = target_info
        self.ref_centric = ref_centric
        self.zoom_in = zoom_in
        self.size_multiple = size_multiple
        self.paired_end_read_length = paired_end_read_length
        self.draw_qualities = draw_qualities
        self.draw_mismatches = draw_mismatches
        self.draw_polyA = draw_polyA
        self.draw_sequence = draw_sequence
        self.max_qual = max_qual
        self.process_mappings = process_mappings
        self.detect_orientation = detect_orientation
        self.label_layout = label_layout
        self.highlight_SNPs = highlight_SNPs
        self.highlight_around_cut = highlight_around_cut
        self.reverse_complement = reverse_complement
        self.label_left = label_left
        self.flip_donor = flip_donor
        self.flip_target = flip_target
        self.ax = ax
        
        if self.ref_centric:
            self.gap_between_als = 0.003
        else:
            self.gap_between_als = 0.012

        self.arrow_height = 0.005
        self.arrow_width = 5

        self.text_y = -7

        self.cross_x = 0.5
        self.cross_y = 0.001

        self.query_length = alignments[0].query_length
        self.query_name = alignments[0].query_name
        
        if self.ax is None:
            self.fig, self.ax = plt.subplots()
        else:
            self.fig = self.ax.figure
        
        if self.label_left:
            self.label_x = 0
            self.label_ha = 'right'
            self.label_x_offset = -30
        else:
            self.label_x = 1
            self.label_ha = 'left'
            self.label_x_offset = 20
        
        if self.reverse_complement is None:
            if self.paired_end_read_length is not None:
                self.reverse_complement = False

            elif self.detect_orientation and not all(al.is_unmapped for al in self.alignments):
                layout = layout_module.Layout(alignments, self.target_info)
                self.reverse_complement = (layout.strand == '-')

            else:
                self.reverse_complement = False
        
        self.ref_name_to_color = defaultdict(lambda: 'grey')
        for i, name in enumerate(self.target_info.reference_sequences):
            self.ref_name_to_color[name] = 'C{0}'.format(i)

        self.max_y = self.gap_between_als
        self.min_y = -self.gap_between_als 

        self.alignment_coordinates = defaultdict(list)

        self.plot_read()
        if self.ref_centric:
            self.draw_target_and_donor()
        self.update_size()

    def draw_read_arrows(self):
        ''' Draw black arrows that represent the sequencing read or read pair. '''
        arrow_kwargs = {
            'linewidth': 2,
            'color': 'black'
        }

        if self.paired_end_read_length is not None:
            offsets = [0.0007, -0.0007]

            # Cap overhang at a fraction of the overlap length.
            capped_length = min(self.paired_end_read_length, self.query_length * 1.25)

            # If there is an overhang, shift the label down so it doesn't collide.
            if capped_length > self.query_length:
                label_y_offset = -10
            else:
                label_y_offset = 0

            endpoints = [
                [0, capped_length],
                [self.query_length - 1, self.query_length - 1 - capped_length],
            ]

            signs = [
                1,
                -1,
            ]

            for (start, end), sign, offset in zip(endpoints, signs, offsets):
                arrow_xs = [start, end, end - sign * self.arrow_width]
                arrow_ys = [offset, offset, offset + sign * self.arrow_height]
                self.ax.plot(arrow_xs, arrow_ys, clip_on=False, **arrow_kwargs)

            read_label = 'read pair'

        else:
            self.ax.plot([0, self.query_length - 1], [0, 0], **arrow_kwargs)

            arrow_ys = [0, self.arrow_height]

            if self.reverse_complement:
                arrow_xs = [0, self.arrow_width]
            else:
                arrow_xs = [self.query_length - 1, self.query_length - 1 - self.arrow_width]
            
            self.ax.plot(arrow_xs, arrow_ys, **arrow_kwargs)

            read_label = 'amplicon'
            label_y_offset = 0

        # Draw label on read.
        self.ax.annotate(read_label,
                         xy=(self.label_x, 0),
                         xycoords=('axes fraction', 'data'),
                         xytext=(self.label_x_offset, label_y_offset),
                         textcoords='offset points',
                         color='black',
                         ha=self.label_ha,
                         va='center',
                        )

    def draw_alignments(self):
        ax = self.ax
        alignments = [al for al in self.alignments if not al.is_unmapped]

        by_reference_name = defaultdict(list)
        for al in sorted(alignments, key=lambda al: (al.reference_name, sam.query_interval(al))):
            by_reference_name[al.reference_name].append(al)
        
        if self.ref_centric:
            rnames_below = [self.target_info.target]
            initial_offset = 2
        else:
            rnames_below = []
            initial_offset = 1

        rnames_above = [n for n in by_reference_name if n not in rnames_below]

        offsets = {}
        for names, sign in [(rnames_below, -1), (rnames_above, 1)]:
            starts = sign * np.cumsum([initial_offset] + [len(by_reference_name[n]) for n in names])
            for name, start in zip(names, starts):
                offsets[name] = start

        for ref_name, ref_alignments in by_reference_name.items():
            if self.reverse_complement:
                for alignment in ref_alignments:
                    alignment.is_reverse = not alignment.is_reverse

            ref_alignments = ref_alignments[:20]
            
            offset = offsets[ref_name]
            color = self.ref_name_to_color[ref_name]

            average_y = (offset  + 0.5 * (len(ref_alignments) - 1)) * self.gap_between_als
            if not self.ref_centric:
                ax.annotate(ref_name,
                            xy=(self.label_x, average_y),
                            xycoords=('axes fraction', 'data'),
                            xytext=(self.label_x_offset, 0),
                            textcoords='offset points',
                            color=color,
                            ha=self.label_ha,
                            va='center',
                        )
                        
            for i, alignment in enumerate(ref_alignments):
                start, end = sam.query_interval(alignment)
                strand = sam.get_strand(alignment)
                y = (offset + i * np.sign(offset)) * self.gap_between_als
                
                # Annotate the ends of alignments with reference position numbers and vertical lines.
                for x, which in ((start, 'start'), (end, 'end')):
                    if (which == 'start' and strand == '+') or (which == 'end' and strand == '-'):
                        r = alignment.reference_start
                    else:
                        r = alignment.reference_end - 1

                    ax.plot([x, x], [0, y], color=color, alpha=0.3)

                    if which == 'start':
                        kwargs = {'ha': 'right', 'xytext': (-2, 0)}
                    else:
                        kwargs = {'ha': 'left', 'xytext': (2, 0)}

                    ax.annotate('{0:,}'.format(r),
                                xy=(x, y),
                                xycoords='data',
                                textcoords='offset points',
                                color=color,
                                va='center',
                                size=6,
                                **kwargs)

                if self.draw_mismatches:
                    mismatches = get_mismatch_info(alignment, self.target_info)
                    for read_p, read_b, ref_p, ref_b, q in mismatches:
                        if q < self.max_qual * 0.75:
                            alpha = 0.25
                        else:
                            alpha = 0.85

                        print(q, self.max_qual, alpha)

                        cross_kwargs = dict(zorder=10, color='black', alpha=alpha)
                        cross_ys = [y - self.cross_y, y + self.cross_y]
                        ax.plot([read_p - self.cross_x, read_p + self.cross_x], cross_ys, **cross_kwargs)
                        ax.plot([read_p + self.cross_x, read_p - self.cross_x], cross_ys, **cross_kwargs)

                # Draw the alignment, with downward dimples at insertions and upward loops at deletions.
                xs = [start]
                ys = [y]
                indels = sorted(get_indel_info(alignment), key=lambda t: t[1][0])
                for kind, info in indels:
                    if kind == 'deletion':
                        centered_at, length = info

                        # Cap how wide the loop can be.
                        capped_length = min(100, length)
                        
                        if length <= 1:
                            height = 0.0015
                            indel_xs = [centered_at, centered_at, centered_at]
                            indel_ys = [y, y + height, y]
                        else:
                            width = self.query_length * 0.001
                            height = 0.006

                            indel_xs = [
                                centered_at - width,
                                centered_at - 0.5 * capped_length,
                                centered_at + 0.5 * capped_length,
                                centered_at + width,
                            ]
                            indel_ys = [y, y + height, y + height, y]

                            ax.annotate(str(length),
                                        xy=(centered_at, y + height),
                                        xytext=(0, 1),
                                        textcoords='offset points',
                                        ha='center',
                                        va='bottom',
                                        size=6,
                                    )

                    elif kind == 'insertion':
                        starts_at, ends_at = info
                        centered_at = np.mean([starts_at, ends_at])
                        length = ends_at - starts_at
                        if length <= 2:
                            height = 0.0015
                        else:
                            height = 0.004
                            ax.annotate(str(length),
                                        xy=(centered_at, y - height),
                                        xytext=(0, -1),
                                        textcoords='offset points',
                                        ha='center',
                                        va='top',
                                        size=6,
                                    )
                        indel_xs = [starts_at, centered_at, ends_at]
                        indel_ys = [y, y - height, y]
                        
                    xs.extend(indel_xs)
                    ys.extend(indel_ys)
                    
                xs.append(end)
                ys.append(y)

                ref_ps = (alignment.reference_start, alignment.reference_end - 1)
                if alignment.is_reverse:
                    ref_ps = ref_ps[::-1]

                coordinates = [
                    (start, end),
                    ref_ps,
                    y,
                ]
                self.alignment_coordinates[ref_name].append(coordinates)
                
                self.max_y = max(self.max_y, max(ys))
                self.min_y = min(self.min_y, min(ys))
                
                kwargs = {'color': color, 'linewidth': 1.5}
                ax.plot(xs, ys, **kwargs)
                
                if strand == '+':
                    arrow_xs = [end, end - self.arrow_width]
                    arrow_ys = [y, y + self.arrow_height]
                else:
                    arrow_xs = [start, start + self.arrow_width]
                    arrow_ys = [y, y - self.arrow_height]
                    
                draw_arrow = True
                if self.zoom_in is not None:
                    if not all(self.min_x <= x <= self.max_x for x in arrow_xs):
                        draw_arrow = False

                if draw_arrow:
                    ax.plot(arrow_xs, arrow_ys, clip_on=False, **kwargs)

                features = copy.deepcopy(self.target_info.features)
                donor = self.target_info.donor

                features_to_show = [(r_name, f_name) for r_name, f_name in features
                                    if r_name == ref_name and 'edge' not in f_name and 'SNP' not in f_name]

                q_to_r = {sam.true_query_position(q, alignment): r
                          for q, r in alignment.aligned_pairs
                          if r is not None and q is not None
                         }

                if self.highlight_SNPs:
                    SNP_names = [(s_n, f_n) for s_n, f_n in features if s_n == ref_name and f_n.startswith('SNP')]
                    for feature_reference, feature_name in SNP_names:
                        feature = features[feature_reference, feature_name]
                        
                        qs = [q for q, r in q_to_r.items() if feature.start <= r <= feature.end]
                        if len(qs) != 1:
                            continue

                        q = qs[0]

                        left_x = q - 0.5
                        right_x = q + 0.5
                        bottom_y = y - (self.cross_y * 2)
                        top_y = y + (self.cross_y * 2)
                        path_xs = [left_x, right_x, right_x, left_x]
                        path_ys = [bottom_y, bottom_y, top_y, top_y]
                        path = np.array([path_xs, path_ys]).T
                        patch = plt.Polygon(path, color='black', alpha=0.2, linewidth=0)
                        ax.add_patch(patch)
                        
                if self.highlight_around_cut:
                    features.update(self.target_info.around_cut_features)
                    features_to_show.update(list(self.target_info.around_cut_features))
                    features_to_show.remove((donor, self.target_info.knockin))

                for feature_reference, feature_name in features_to_show:
                    if ref_name != feature_reference:
                        continue

                    if (feature_reference, feature_name) not in features:
                        continue

                    feature = features[feature_reference, feature_name]
                    feature_color = feature.attribute['color']
                    
                    qs = [q for q, r in q_to_r.items() if feature.start <= r <= feature.end]
                    if not qs:
                        continue

                    xs = [min(qs), max(qs)]
                    
                    rs = [feature.start, feature.end]
                    if strand == '-':
                        rs = rs[::-1]

                    if np.sign(offset) == 1:
                        va = 'bottom'
                        text_y = 1
                    else:
                        va = 'top'
                        text_y = -1

                    if not self.ref_centric:
                        for ha, q, r in zip(['left', 'right'], xs, rs):
                            nts_missing = abs(q_to_r[q] - r)
                            if nts_missing != 0 and xs[1] - xs[0] > 20:
                                ax.annotate(str(nts_missing),
                                            xy=(q, 0),
                                            ha=ha,
                                            xytext=(3 if ha == 'left' else -3, text_y),
                                            textcoords='offset points',
                                            size=6,
                                            va=va,
                                        )
                        
                    ax.fill_between(xs, [y] * 2, [0] * 2, color=feature_color, alpha=0.7)
                        
                    if not self.ref_centric:
                        if xs[1] - xs[0] > 18 or feature.attribute['ID'] == self.target_info.sgRNA:
                            ax.annotate(feature.attribute['ID'],
                                        xy=(np.mean(xs), 0),
                                        xycoords='data',
                                        xytext=(0, self.text_y),
                                        textcoords='offset points',
                                        va='top',
                                        ha='center',
                                        color=feature_color,
                                        size=10,
                                        weight='bold',
                                    )

    def plot_read(self):
        ax = self.ax
        alignments = self.alignments

        if (not alignments) or (alignments[0].query_sequence is None):
            return self.fig

        if self.process_mappings is not None:
            layout_info = self.process_mappings(alignments, self.target_info)
            alignments = layout_info['to_plot']

        if self.zoom_in is not None:
            self.min_x = self.zoom_in[0] * self.query_length
            self.max_x = self.zoom_in[1] * self.query_length
        else:
            self.min_x = -0.02 * self.query_length
            self.max_x = 1.02 * self.query_length
        
        self.draw_read_arrows()

        self.draw_alignments()

        if self.label_layout:
            layout = layout_module.Layout(alignments, self.target_info)
            cat, subcat, details = layout.categorize()
            title = '{}\n{}, {}, {}'.format(self.query_name, cat, subcat, details)
        else:
            title = self.query_name

        ax.set_title(title, y=1.2)
            
        ax.set_ylim(1.1 * self.min_y, 1.1 * self.max_y)
        ax.set_xlim(self.min_x, self.max_x)
        ax.set_yticks([])
        
        ax.spines['bottom'].set_position(('data', 0))
        ax.spines['bottom'].set_alpha(0.1)
        for edge in 'left', 'top', 'right':
            ax.spines[edge].set_color('none')
            
        if not self.ref_centric:
            ax.tick_params(pad=14)

        if self.draw_qualities:
            quals = alignments[0].query_qualities
            if alignments[0].is_reverse:
                quals = quals[::-1]

            qual_ys = np.array(quals) * self.max_y / self.max_qual
            ax.plot(qual_ys, color='black', alpha=0.5)

        if self.draw_polyA:
            seq = alignments[0].get_forward_sequence()
            for b, color in [('A', 'red'), ('G', 'brown')]:
                locations = utilities.homopolymer_lengths(seq, b)
                for start, length in locations:
                    if length > 10:
                        ax.fill_between([start, start + length - 1], [self.max_y + self.arrow_height] * 2, [0] * 2, color=color, alpha=0.2)
                        
                        ax.annotate('poly{}'.format(b),
                                    xy=(start + length / 2, 0),
                                    xycoords='data',
                                    xytext=(0, self.text_y),
                                    textcoords='offset points',
                                    va='top',
                                    ha='center',
                                    color=color,
                                    alpha=0.4,
                                    size=10,
                                    weight='bold',
                                )
                        
        if self.draw_sequence:
            seq = alignments[0].get_forward_sequence()

            for x, b in enumerate(seq):
                if self.min_x <= x <= self.max_x:
                    ax.annotate(b,
                                xy=(x, 0),
                                family='monospace',
                                size=4,
                                xytext=(0, -2),
                                textcoords='offset points',
                                ha='center',
                                va='top',
                            )
            
        return self.fig

    def draw_target_and_donor(self):
        ti = self.target_info
        knockin_feature = ti.features[ti.donor, ti.knockin]

        gap = 0.03
        
        params = [
            (ti.target, ti.cut_after, self.min_y - gap, self.flip_target),
            (ti.donor, np.mean([knockin_feature.start, knockin_feature.end]), self.max_y + gap, self.flip_donor),
        ]

        for name, center_p, ref_y, reverse in params:
            color = self.ref_name_to_color[name]

            if len(self.alignment_coordinates[name]) == 1:
                xs, ps, y = self.alignment_coordinates[name][0]
                anchor_ref = ps[0]
                anchor_read = xs[0]
            else:
                anchor_ref = center_p
                anchor_read = self.query_length // 2

            if reverse:
                ref_p_to_x = lambda p: anchor_read - (p - anchor_ref)
                x_to_ref_p = lambda x: (anchor_read - x) + anchor_ref
            else:
                ref_p_to_x = lambda p: (p - anchor_ref) + anchor_read
                x_to_ref_p = lambda x: (x - anchor_read) + anchor_ref

            ref_edge = len(ti.reference_sequences[name]) - 1

            ref_start = max(0, x_to_ref_p(self.min_x))
            ref_end = min(ref_edge, x_to_ref_p(self.max_x))

            ref_al_min = ref_start
            ref_al_max = ref_end

            for xs, ps, y in self.alignment_coordinates[name]:
                ref_xs = [ref_p_to_x(p) for p in ps]
                ref_al_min = min(ref_al_min, min(ps))
                ref_al_max = max(ref_al_max, max(ps))

                self.ax.fill_betweenx([y, ref_y], [xs[0], ref_xs[0]], [xs[1], ref_xs[1]], color=color, alpha=0.05)

                for x, ref_x in zip(xs, ref_xs):
                    self.ax.plot([x, ref_x], [y, ref_y], color=color, alpha=0.3)

            self.min_y = min(self.min_y, ref_y)
            self.max_y = max(self.max_y, ref_y)

            if ref_al_min < ref_start:
                ref_start = max(0, ref_al_min - 30, ref_start - 30)
                self.min_x = min(self.min_x, ref_p_to_x(ref_start))

            if ref_al_max > ref_end:
                ref_end = min(ref_edge, ref_al_max + 30, ref_end + 30)
                self.max_x = max(self.max_x, ref_p_to_x(ref_end))

            self.ax.set_xlim(self.min_x, self.max_x)

            ref_xs = [ref_p_to_x(ref_start), ref_p_to_x(ref_end)]

            self.ax.plot(ref_xs, [ref_y, ref_y], color=color, linewidth=3, solid_capstyle='butt')

            features_to_show = [(r_name, f_name) for r_name, f_name in ti.features
                                if r_name == name and 'edge' not in f_name and 'SNP' not in f_name]

            for feature_reference, feature_name in features_to_show:
                feature = ti.features[feature_reference, feature_name]
                feature_color = feature.attribute['color']
                
                xs = [ref_p_to_x(p) for p in [feature.start, feature.end]]
                    
                start = ref_y
                end = ref_y + np.sign(ref_y) * gap * 0.2

                self.ax.fill_between(xs, [start] * 2, [end] * 2, color=feature_color, alpha=0.7, linewidth=0)

                self.ax.annotate(feature.attribute['ID'],
                                 xy=(np.mean(xs), end),
                                 xycoords='data',
                                 xytext=(0, 2 * np.sign(ref_y)),
                                 textcoords='offset points',
                                 va='top' if ref_y < 0 else 'bottom',
                                 ha='center',
                                 color=feature_color,
                                 size=10,
                                 weight='bold',
                                )

            # Draw target and donor names next to diagrams.
            self.ax.annotate(name,
                             xy=(self.label_x, ref_y),
                             xycoords=('axes fraction', 'data'),
                             xytext=(self.label_x_offset, 0),
                             textcoords='offset points',
                             color=color,
                             ha=self.label_ha,
                             va='center',
                            )
            
        self.ax.set_ylim(self.min_y - 0.1 * self.height, self.max_y + 0.1 * self.height)

    @property
    def height(self):
        return self.max_y - self.min_y
    
    @property
    def width(self):
        return self.max_x - self.min_x

    def update_size(self):
        fig_width = 0.04 * (self.width + 50) * self.size_multiple
        fig_height = 40 * self.height * self.size_multiple
        self.fig.set_size_inches((fig_width, fig_height))

def plot_pair(R1_als, R2_als, target_info, title=None, **kwargs):
    fig, axs = plt.subplots(1, 2, gridspec_kw={'wspace': 0.1})

    plot_read(R1_als, target_info, ax=axs[0], label_left=True, **kwargs)
    plot_read(R2_als, target_info, ax=axs[1], reverse_complement=True, **kwargs)

    y_min = 0
    y_max = 0
    for ax in axs:
        this_y_min, this_y_max = ax.get_ylim()
        y_min = min(y_min, this_y_min)
        y_max = max(y_max, this_y_max)

    for ax in axs:
        ax.set_ylim(y_min, y_max)

    axs[0].set_title('R1')
    axs[1].set_title('R2')
    if title is not None:
        fig.suptitle(title, y=2)

    fig.set_size_inches((24, 24 * y_max))
    
    return fig

def make_stacked_Image(als_iter, target_info, titles=None, pairs=False, **kwargs):
    if titles is None:
        titles = itertools.repeat(None)
    ims = []

    for als, title in zip(als_iter, titles):
        if als is None:
            continue
            
        if pairs:
            R1_als, R2_als = als
            fig = plot_pair(R1_als, R2_als, target_info, title=title, **kwargs)
        else:
            fig = plot_read(als, target_info, **kwargs)

            if title is not None:
                fig.axes[0].set_title(title)
        #fig.axes[0].set_title('_', y=1.2, color='white')
        
        with io.BytesIO() as buffer:
            fig.savefig(buffer, format='png', bbox_inches='tight')
            im = PIL.Image.open(buffer)
            im.load()
            ims.append(im)
        plt.close(fig)
        
    if not ims:
        return None

    total_height = sum(im.height for im in ims)
    max_width = max(im.width for im in ims)

    stacked_im = PIL.Image.new('RGBA', size=(max_width, total_height), color='white')
    y_start = 0
    for im in ims:
        stacked_im.paste(im, (max_width - im.width, y_start))
        y_start += im.height

    return stacked_im

def make_length_plot(read_lengths, color, outcome_lengths=None, max_length=None):
    def plot_nonzero(ax, xs, ys, color, highlight):
        nonzero = ys.nonzero()
        if highlight:
            alpha = 0.95
            markersize = 2
        else:
            alpha = 0.7
            markersize = 0

        ax.plot(xs[nonzero], ys[nonzero], 'o', color=color, markersize=markersize, alpha=alpha)
        ax.plot(xs, ys, '-', color=color, alpha=0.3 * alpha)

    fig, ax = plt.subplots(figsize=(14, 5))

    ys = read_lengths
    xs = np.arange(len(ys))

    if outcome_lengths is None:
        all_color = color
        highlight = True
    else:
        all_color = 'black'
        highlight = False

    plot_nonzero(ax, xs, ys, all_color, highlight=highlight)
    ax.set_ylim(0, max(ys) * 1.05)

    if outcome_lengths is not None:
        ys = outcome_lengths
        xs = np.arange(len(ys))
        outcome_color = color
        plot_nonzero(ax, xs, ys, outcome_color, highlight=True)

    ax.set_xlabel('Length of read')
    ax.set_ylabel('Number of reads')
    ax.set_xlim(0, len(read_lengths) * 1.05)
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))

    return fig