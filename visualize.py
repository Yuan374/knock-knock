import matplotlib
matplotlib.use('Agg', warn=False)

import copy
import io
import PIL
import itertools
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker
import ipywidgets
import pandas as pd

from sequencing import utilities, interval, sam

from . import experiment as experiment_module
from . import target_info as target_info_module
from . import layout as layout_module
from . import pooled_layout

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

def plot_read(alignments,
              target_info,
              parsimonious=False,
              show_qualities=False,
              zoom_in=None,
              size_multiple=1,
              paired_end_read_length=None,
              draw_mismatches=True,
              show_polyA=False,
              show_sequence=False,
              max_qual=41,
              process_mappings=None,
              detect_orientation=False,
              label_layout=False,
              highlight_SNPs=False,
              highlight_around_cut=False,
              reverse_complement=None,
              label_left=False,
              ax=None,
              **kwargs):

    alignments = copy.deepcopy(alignments)

    if ax is None:
        fig, ax = plt.subplots(figsize=(12, 4))
    else:
        fig = ax.figure

    if (not alignments) or (alignments[0].query_sequence is None):
        return fig

    colors = {name: 'C{0}'.format(i) for i, name in enumerate(target_info.reference_sequences)}

    if reverse_complement is None:
        if paired_end_read_length is not None:
            reverse_complement = False

        elif detect_orientation and not all(al.is_unmapped for al in alignments):
            layout = layout_module.Layout(alignments, target_info)
            reverse_complement = (layout.strand == '-')

        else:
            reverse_complement = False

    if process_mappings is not None:
        layout_info = process_mappings(alignments, target_info)
        alignments = layout_info['to_plot']

    gap_between_als = 0.06 * 0.2
    arrow_height = 0.005
    arrow_width = 0.01
    text_y = -7

    cross_x = 1 * 0.5
    cross_y = cross_x * 0.002
    
    max_y = gap_between_als
    
    if parsimonious:
        alignments = interval.make_parsimonious(alignments)
        
    query_name = alignments[0].query_name
    query_length = alignments[0].query_length
    
    if zoom_in is not None:
        x_min = zoom_in[0] * query_length
        x_max = zoom_in[1] * query_length
    else:
        x_min = -0.02 * query_length
        x_max = 1.02 * query_length
    
    read_kwargs = {
        'linewidth': 2,
        'color': 'black'
    }

    if paired_end_read_length is not None:
        offsets = [
            #0,
            #-gap_between_als * 0.15,
            gap_between_als * 0.07,
            -gap_between_als * 0.07,
        ]

        # Cap overhang at a fraction of the overlap length.
        capped_length = min(paired_end_read_length, query_length * 1.25)

        # If there is an overhang, shift the label down so it doesn't collide.
        if capped_length > query_length:
            label_y_offset = -10
        else:
            label_y_offset = 0

        endpoints = [
            [0, capped_length],
            [query_length - 1, query_length - 1 - capped_length],
        ]

        signs = [
            1,
            -1,
        ]

        for (start, end), sign, offset in zip(endpoints, signs, offsets):
            ax.plot([start, end, end - sign * query_length * arrow_width],
                    [offset, offset, offset + sign * arrow_height],
                    clip_on=False,
                    **read_kwargs)

        read_label = 'read pair'

    else:
        ax.plot([0, query_length - 1], [0, 0], **read_kwargs)

        arrow_ys = [0, arrow_height]

        if reverse_complement:
            arrow_xs = [0, (query_length - 1) * arrow_width]
        else:
            arrow_xs = [query_length - 1, (query_length - 1) * (1 - arrow_width)]
        
        ax.plot(arrow_xs, arrow_ys, **read_kwargs)

        read_label = 'sequencing read'
        label_y_offset = 0

    if label_left:
        label_x = 0
        label_ha = 'right'
        label_x_offset = -30
    else:
        label_x = 1
        label_ha = 'left'
        label_x_offset = 20

    ax.annotate(read_label,
                xy=(label_x, 0),
                xycoords=('axes fraction', 'data'),
                xytext=(label_x_offset, label_y_offset),
                textcoords='offset points',
                color='black',
                ha=label_ha,
                va='center',
               )

    if all(al.is_unmapped for al in alignments):
        by_reference_name = []
    else:
        alignments = [al for al in alignments if not al.is_unmapped]
        alignments = sorted(alignments, key=lambda al: (al.reference_name, sam.query_interval(al)))
        by_reference_name = list(utilities.group_by(alignments, lambda al: al.reference_name))
    
    rname_starts = np.cumsum([1] + [len(als) for n, als in by_reference_name])
    offsets = {name: start for (name, als), start in zip(by_reference_name, rname_starts)}

    for ref_name, ref_alignments in by_reference_name:
        if reverse_complement:
            for alignment in ref_alignments:
                alignment.is_reverse = not alignment.is_reverse

        ref_alignments = ref_alignments[:20]
        
        offset = offsets[ref_name]
        color = colors.get(ref_name, 'grey')

        average_y = (offset  + 0.5 * (len(ref_alignments) - 1)) * gap_between_als
        ax.annotate(ref_name,
                    xy=(label_x, average_y),
                    xycoords=('axes fraction', 'data'),
                    xytext=(label_x_offset, 0),
                    textcoords='offset points',
                    color=color,
                    ha=label_ha,
                    va='center',
                   )
                    
        for i, alignment in enumerate(ref_alignments):
            start, end = sam.query_interval(alignment)
            strand = sam.get_strand(alignment)
            y = (offset + i) * gap_between_als
            
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

            if draw_mismatches:
                mismatches = get_mismatch_info(alignment, target_info)
                for read_p, read_b, ref_p, ref_b, q in mismatches:
                    if q < max_qual * 0.75:
                        alpha = 0.25
                    else:
                        alpha = 0.85

                    cross_kwargs = dict(zorder=10, color='black', alpha=alpha)
                    ax.plot([read_p - cross_x, read_p + cross_x], [y - cross_y, y + cross_y], **cross_kwargs)
                    ax.plot([read_p + cross_x, read_p - cross_x], [y - cross_y, y + cross_y], **cross_kwargs)

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
                        width = query_length * 0.001
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
            
            max_y = max(max_y, max(ys))
            
            kwargs = {'color': color, 'linewidth': 1.5}
            ax.plot(xs, ys, **kwargs)
            
            if strand == '+':
                arrow_xs = [end, end - query_length * arrow_width]
                arrow_ys = [y, y + arrow_height]
            else:
                arrow_xs = [start, start + query_length * arrow_width]
                arrow_ys = [y, y - arrow_height]
                
            draw_arrow = True
            if zoom_in is not None:
                if not all(x_min <= x <= x_max for x in arrow_xs):
                    draw_arrow = False

            if draw_arrow:
                ax.plot(arrow_xs, arrow_ys, clip_on=False, **kwargs)

            features = target_info.features
            target = target_info.target
            donor = target_info.donor

            features_to_show = {
                (target, target_info.primer_names[5]),
                (target, target_info.primer_names[3]),
                (donor, target_info.knockin),
                (target, 'PAS'),
                (donor, 'GFP11'),
            }

            q_to_r = {sam.true_query_position(q, alignment): r
                      for q, r in alignment.aligned_pairs
                      if r is not None and q is not None
                     }

            if highlight_SNPs:
                SNP_names = [(s_n, f_n) for s_n, f_n in target_info.features if s_n  == ref_name and f_n.startswith('SNP')]
                for feature_reference, feature_name in SNP_names:
                    feature = features[feature_reference, feature_name]
                    
                    qs = [q for q, r in q_to_r.items() if feature.start <= r <= feature.end]
                    if len(qs) != 1:
                        continue

                    q = qs[0]

                    left_x = q - 0.5
                    right_x = q + 0.5
                    bottom_y = y - (cross_y * 2)
                    top_y = y + (cross_y * 2)
                    path_xs = [left_x, right_x, right_x, left_x]
                    path_ys = [bottom_y, bottom_y, top_y, top_y]
                    path = np.array([path_xs, path_ys]).T
                    patch = plt.Polygon(path, color='black', alpha=0.2, linewidth=0)
                    ax.add_patch(patch)
                    
            if highlight_around_cut:
                target_info.features.update(target_info.around_cut_features)
                features_to_show.update(list(target_info.around_cut_features))
            else:
                features_to_show.update([
                    (target, "3' HA"),
                    (target, "5' HA"),
                    (target, target_info.sgRNA),
                    (donor, "3' HA"),
                    (donor, "5' HA"),
                ])

                features_to_show.update([(target, name) for name in target_info.sgRNAs])

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
                if xs[1] - xs[0] < 5:
                    continue
                
                rs = [feature.start, feature.end]
                if strand == '-':
                    rs = rs[::-1]
                    
                for ha, q, r in zip(['left', 'right'], xs, rs):
                    nts_missing = abs(q_to_r[q] - r)
                    if nts_missing != 0 and xs[1] - xs[0] > 20:
                        ax.annotate(str(nts_missing),
                                    xy=(q, 0),
                                    ha=ha,
                                    va='bottom',
                                    xytext=(3 if ha == 'left' else -3, 1),
                                    textcoords='offset points',
                                    size=6,
                                   )
                        
                    
                ax.fill_between(xs, [y] * 2, [0] * 2, color=feature_color, alpha=0.7)
                
                if xs[1] - xs[0] > 18 or feature.attribute['ID'] == target_info.sgRNA:
                    ax.annotate(feature.attribute['ID'],
                                xy=(np.mean(xs), 0),
                                xycoords='data',
                                xytext=(0, text_y),
                                textcoords='offset points',
                                va='top',
                                ha='center',
                                color=feature_color,
                                size=10,
                                weight='bold',
                               )

    if label_layout:
        layout = layout_module.Layout(alignments, target_info)
        cat, subcat, details = layout.categorize()
        title = '{}\n{}, {}, {}'.format(query_name, cat, subcat, details)
    else:
        title = query_name

    ax.set_title(title, y=1.2)
        
    ax.set_ylim(-0.2 * max_y, 1.1 * max_y)
    ax.set_xlim(x_min, x_max)
    ax.set_yticks([])
    
    ax.spines['bottom'].set_position(('data', 0))
    ax.spines['bottom'].set_alpha(0.1)
    for edge in 'left', 'top', 'right':
        ax.spines[edge].set_color('none')
        
    ax.tick_params(pad=14)
    fig.set_size_inches((18 * size_multiple, 40 * max_y * size_multiple))
    
    if show_qualities:
        quals = alignments[0].query_qualities
        if alignments[0].is_reverse:
            quals = quals[::-1]

        ax.plot(np.array(quals) * max_y / max_qual, color='black', alpha=0.5)

    if show_polyA:
        seq = alignments[0].get_forward_sequence()
        for b, color in [('A', 'red'), ('G', 'brown')]:
            locations = utilities.homopolymer_lengths(seq, b)
            for start, length in locations:
                if length > 10:
                    ax.fill_between([start, start + length - 1], [max_y + arrow_height] * 2, [0] * 2, color=color, alpha=0.2)
                    
                    ax.annotate('poly{}'.format(b),
                                xy=(start + length / 2, 0),
                                xycoords='data',
                                xytext=(0, text_y),
                                textcoords='offset points',
                                va='top',
                                ha='center',
                                color=color,
                                alpha=0.4,
                                size=10,
                                weight='bold',
                               )
                    
    if show_sequence:
        seq = alignments[0].get_forward_sequence()

        for x, b in enumerate(seq):
            if x_min <= x <= x_max:
                ax.annotate(b,
                            xy=(x, 0),
                            family='monospace',
                            size=4,
                            xytext=(0, -2),
                            textcoords='offset points',
                            ha='center',
                            va='top',
                           )
        
    return fig

def make_stacked_Image(als_iter, target_info, titles=None, **kwargs):
    if titles is None:
        titles = itertools.repeat(None)

    ims = []

    for als, title in zip(als_iter, titles):
        if als is None:
            continue
            
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

def explore_pooled(base_dir, group,
                   initial_guide=None,
                   by_outcome=False,
                   draw_mismatches=False,
                   parsimonious=True,
                   relevant=False,
                   show_sequence=False,
                   size_multiple=1,
                   highlight_SNPs=False,
                   highlight_around_cut=True,
                  ):
    pool = experiment_module.PooledExperiment(base_dir, group)

    guides = pool.guides
    if initial_guide is None:
        initial_guide = guides[0]

    widgets = {
        'guide': ipywidgets.Select(options=guides, value=initial_guide, layout=ipywidgets.Layout(height='200px', width='450px')),
        'read_id': ipywidgets.Select(options=[], layout=ipywidgets.Layout(height='200px', width='600px')),
        'parsimonious': ipywidgets.ToggleButton(value=parsimonious),
        'relevant': ipywidgets.ToggleButton(value=relevant),
        'show_qualities': ipywidgets.ToggleButton(value=False),
        'draw_mismatches': ipywidgets.ToggleButton(value=draw_mismatches),
        'show_sequence': ipywidgets.ToggleButton(value=show_sequence),
        'highlight_SNPs': ipywidgets.ToggleButton(value=highlight_SNPs),
        'highlight_around_cut': ipywidgets.ToggleButton(value=highlight_around_cut),
        'outcome': ipywidgets.Select(options=[], continuous_update=False, layout=ipywidgets.Layout(height='200px', width='450px')),
        'zoom_in': ipywidgets.FloatRangeSlider(value=[-0.02, 1.02], min=-0.02, max=1.02, step=0.001, continuous_update=False, layout=ipywidgets.Layout(width='1200px')),
        'save': ipywidgets.Button(description='Save'),
        'file_name': ipywidgets.Text(value=str(base_dir / 'figures')),
    }

    def save(change):
        fig = interactive.result
        fn = widgets['file_name'].value
        fig.savefig(fn, bbox_inches='tight')

    widgets['save'].on_click(save)

    # For some reason, the target widget doesn't get a label without this.
    for k, v in widgets.items():
        v.description = k

    output = ipywidgets.Output()

    def get_exp():
        guide = widgets['guide'].value
        exp = experiment_module.SingleGuideExperiment(base_dir, group, guide)
        return exp

    @output.capture()
    def populate_outcomes(change):
        previous_value = widgets['outcome'].value

        exp = get_exp()

        outcomes = {(c, sc) for c, sc, d in exp.outcome_counts.index.values}

        widgets['outcome'].options = [('_'.join(outcome), outcome) for outcome in sorted(outcomes)]
        if outcomes:
            if previous_value in outcomes:
                widgets['outcome'].value = previous_value
                populate_read_ids(None)
            else:
                widgets['outcome'].value = widgets['outcome'].options[0][1]
        else:
            widgets['outcome'].value = None

    @output.capture()
    def populate_read_ids(change):
        exp = get_exp()

        df = exp.filtered_cell_outcomes

        if exp is None:
            return

        if by_outcome:
            outcome = widgets['outcome'].value
            if outcome is None:
                qnames = []
            else:
                category, subcategory = outcome
                right_outcome = df.query('category == @category and subcategory == @subcategory')
                qnames = right_outcome['original_name'].values[:200]
        else:
            qnames = df['original_name'].values[:200]

        widgets['read_id'].options = qnames

        if len(qnames) > 0:
            widgets['read_id'].value = qnames[0]
            widgets['read_id'].index = 0
        else:
            widgets['read_id'].value = None
            
    if by_outcome:
        populate_outcomes({'name': 'initial'})

    populate_read_ids({'name': 'initial'})

    if by_outcome:
        widgets['outcome'].observe(populate_read_ids, names='value')
        widgets['guide'].observe(populate_outcomes, names='value')
    else:
        widgets['guide'].observe(populate_read_ids, names='value')

    @output.capture(clear_output=True)
    def plot(guide, read_id, **kwargs):
        exp = get_exp()

        if exp is None:
            return

        if by_outcome:
            als = exp.get_read_alignments(read_id, outcome=kwargs['outcome'])
        else:
            als = exp.get_read_alignments(read_id)

        if als is None:
            return None

        l = pooled_layout.Layout(als, exp.target_info)
        info = l.categorize()
        if kwargs['relevant']:
            als = l.relevant_alignments

        fig = plot_read(als, exp.target_info,
                        size_multiple=size_multiple,
                        paired_end_read_length=exp.paired_end_read_length,
                        **kwargs)

        fig.axes[0].set_title(' '.join((l.name,) + info))

        print(als[0].get_forward_sequence())
        print(als[0].query_name)

        return fig

    # Make a version of the widgets dictionary that excludes non-plot arguments.
    most_widgets = widgets.copy()
    most_widgets.pop('save')
    most_widgets.pop('file_name')

    interactive = ipywidgets.interactive(plot, **most_widgets)
    interactive.update()

    def make_row(keys):
        return ipywidgets.HBox([widgets[k] for k in keys])

    if by_outcome:
        top_row_keys = ['guide', 'outcome', 'read_id']
    else:
        top_row_keys = ['guide', 'read_id']

    layout = ipywidgets.VBox(
        [make_row(top_row_keys),
         make_row(['parsimonious',
                   'relevant',
                   'show_qualities',
                   'draw_mismatches',
                   'show_sequence',
                   'highlight_SNPs',
                   'highlight_around_cut',
                   'save',
                    'file_name',
                   ]),
         #widgets['zoom_in'],
         interactive.children[-1],
         output,
        ],
    )

    return layout
    
def explore(base_dir, by_outcome=False, draw_mismatches=False, parsimonious=True, show_sequence=False, size_multiple=1, max_qual=93):
    target_names = sorted([t.name for t in target_info_module.get_all_targets(base_dir)])

    widgets = {
        'target': ipywidgets.Select(options=target_names, value=target_names[0], layout=ipywidgets.Layout(height='200px')),
        'experiment': ipywidgets.Select(options=[], layout=ipywidgets.Layout(height='200px', width='450px')),
        'read_id': ipywidgets.Select(options=[], layout=ipywidgets.Layout(height='200px', width='600px')),
        'parsimonious': ipywidgets.ToggleButton(value=parsimonious),
        'show_qualities': ipywidgets.ToggleButton(value=False),
        'highlight_around_cut': ipywidgets.ToggleButton(value=False),
        'draw_mismatches': ipywidgets.ToggleButton(value=draw_mismatches),
        'show_sequence': ipywidgets.ToggleButton(value=show_sequence),
        'outcome': ipywidgets.Select(options=[], continuous_update=False, layout=ipywidgets.Layout(height='200px', width='450px')),
        'zoom_in': ipywidgets.FloatRangeSlider(value=[-0.02, 1.02], min=-0.02, max=1.02, step=0.001, continuous_update=False, layout=ipywidgets.Layout(width='1200px')),
    }

    # For some reason, the target widget doesn't get a label without this.
    for k, v in widgets.items():
        v.description = k

    exps = experiment_module.get_all_experiments(base_dir)

    output = ipywidgets.Output()

    @output.capture()
    def populate_experiments(change):
        target = widgets['target'].value
        previous_value = widgets['experiment'].value
        datasets = sorted([('{0}: {1}'.format(exp.group, exp.name), exp)
                           for exp in exps
                           if exp.target_info.name == target
                          ])
        widgets['experiment'].options = datasets

        if datasets:
            if previous_value in datasets:
                widgets['experiment'].value = previous_value
                populate_outcomes(None)
            else:
                widgets['experiment'].index = 0
        else:
            widgets['experiment'].value = None

    @output.capture()
    def populate_outcomes(change):
        previous_value = widgets['outcome'].value
        exp = widgets['experiment'].value
        if exp is None:
            return

        outcomes = exp.outcomes
        widgets['outcome'].options = [('_'.join(outcome), outcome) for outcome in outcomes]
        if outcomes:
            if previous_value in outcomes:
                widgets['outcome'].value = previous_value
                populate_read_ids(None)
            else:
                widgets['outcome'].value = widgets['outcome'].options[0][1]
        else:
            widgets['outcome'].value = None

    @output.capture()
    def populate_read_ids(change):
        exp = widgets['experiment'].value

        if exp is None:
            return

        if by_outcome:
            outcome = widgets['outcome'].value
            if outcome is None:
                qnames = []
            else:
                qnames = exp.outcome_query_names(outcome)[:200]
        else:
            qnames = list(itertools.islice(exp.query_names, 200))

        widgets['read_id'].options = qnames

        if qnames:
            widgets['read_id'].value = qnames[0]
            widgets['read_id'].index = 0
        else:
            widgets['read_id'].value = None
            
    populate_experiments({'name': 'initial'})
    if by_outcome:
        populate_outcomes({'name': 'initial'})
    populate_read_ids({'name': 'initial'})

    widgets['target'].observe(populate_experiments, names='value')

    if by_outcome:
        widgets['outcome'].observe(populate_read_ids, names='value')
        widgets['experiment'].observe(populate_outcomes, names='value')
    else:
        widgets['experiment'].observe(populate_read_ids, names='value')

    @output.capture(clear_output=True)
    def plot(experiment, read_id, **kwargs):
        exp = experiment

        if exp is None:
            return

        if by_outcome:
            als = exp.get_read_alignments(read_id, outcome=kwargs['outcome'])
        else:
            als = exp.get_read_alignments(read_id)

        if als is None:
            return None

        fig = plot_read(als, exp.target_info,
                        size_multiple=size_multiple,
                        max_qual=max_qual,
                        paired_end_read_length=exp.paired_end_read_length,
                        **kwargs)

        if kwargs['show_sequence']:
            print(als[0].query_name)
            print(als[0].get_forward_sequence())

        return fig

    interactive = ipywidgets.interactive(plot, **widgets)
    interactive.update()

    def make_row(keys):
        return ipywidgets.HBox([widgets[k] for k in keys])

    if by_outcome:
        top_row_keys = ['target', 'experiment', 'outcome', 'read_id']
    else:
        top_row_keys = ['target', 'experiment', 'read_id']

    layout = ipywidgets.VBox(
        [make_row(top_row_keys),
         make_row(['parsimonious',
                   'show_qualities',
                   'draw_mismatches',
                   'show_sequence',
                   'highlight_around_cut',
                   ]),
         #widgets['zoom_in'],
         interactive.children[-1],
         output,
        ],
    )

    return layout

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