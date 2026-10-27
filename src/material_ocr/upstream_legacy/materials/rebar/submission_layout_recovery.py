"""Additional B.0.6 body layout identified by measured underline topology.

The original single-line form remains first choice. This fallback requires
three date segments, a separate right-hand location line, and a lower material
line inside an already verified outer form and signature separator.
"""
from statistics import median


def stacked_body_underlines(segments, config):
    w,h=config["frame_size"]
    pad=config["header_pad"]
    lo,hi=config["underline_y_range"]
    segments=[s for s in segments if s[3]<20 and lo<=(s[1]-pad)/h<=hi]
    bands=[]
    for s in sorted(segments,key=lambda s:s[1]):
        near=[b for b in bands if abs(s[1]-median(t[1] for t in b))<=6]
        if len(near)==1:
            near[0].append(s)
        else:
            bands.append([s])
    candidates=[]
    for band in bands:
        ordered=sorted(band,key=lambda s:s[0])
        if len(ordered)!=4:
            continue
        date_parts,location=ordered[:3],ordered[3]
        # Date components separated by printed 年/月; do not bridge arbitrary
        # large text regions or consume a neighbouring field's underline.
        if (any(s[2]>.16*w for s in date_parts) or location[2]<.25*w
            or location[0]<.4*w or date_parts[0][0]>.25*w):
            continue
        date_end=date_parts[-1][0]+date_parts[-1][2]
        gaps=[b[0]-a[0]-a[2] for a,b in zip(date_parts,date_parts[1:])]
        if not all(0<g<.08*w for g in gaps) or location[0]-date_end<.1*w:
            continue
        upper_y=median(s[1] for s in band)
        lower=[b for b in bands if len(b)==1
               and .02*h<median(s[1] for s in b)-upper_y<.09*h]
        for b in lower:
            material=b[0]
            if (material[0]>.2*w or material[0]+material[2]>=location[0]
                or not .2*w<material[2]<.65*w):
                continue
            date=(date_parts[0][0],int(median(s[1] for s in date_parts)),
                  date_end-date_parts[0][0],max(s[3] for s in date_parts))
            selected=dict(supply_date=date,use_location=location,material_name=material)
            # Keep each crop above its own underline, below the previous text row.
            field_height=min(config["field_height_px"],round((material[1]-upper_y)*.65))
            candidates.append((selected,field_height,date_parts))
    return candidates[0] if len(candidates)==1 else None
