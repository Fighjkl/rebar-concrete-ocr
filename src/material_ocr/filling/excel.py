"""Write a reviewed ledger and its per-field provenance to a new XLSX file."""
from datetime import datetime
import json
from pathlib import Path

from material_ocr.filling.base import ApprovedFormWriter
from material_ocr.filling.ledger import preview_ledger, validate_values


class RebarLedgerWriter(ApprovedFormWriter):
    def preview(self, result, review_state):
        return preview_ledger(result, review_state)

    def write(self, approved_records, template, output):
        from material_ocr.materials.rebar.filling import COLUMNS
        import xlsxwriter
        if template is not None:
            raise ValueError('当前台账按说明文档截图生成，尚不支持自定义模板')
        if not approved_records:
            raise ValueError('没有可导出的已确认台账行')
        seen = set()
        for row in approved_records:
            if row.get('approved') is not True or not row.get('confirmed_at') or not row.get('sources') or not row.get('signature'):
                raise ValueError('台账行尚未确认或缺少来源')
            identity = row.get('pdf_sha256'), row.get('row_id')
            if identity in seen:
                raise ValueError('同一独立铭牌不能导出为多行')
            seen.add(identity)
            _, errors = validate_values(row['values'], COLUMNS)
            if errors:
                raise ValueError('；'.join(errors))
        output = Path(output)
        if output.exists():
            raise ValueError('导出文件已存在，不能覆盖')
        output.parent.mkdir(parents=True, exist_ok=True)
        with xlsxwriter.Workbook(output, {'strings_to_formulas': False, 'strings_to_urls': False}) as book:
            book.set_properties({'title': '材料报审台账', 'comments': '一独立铭牌一行；仅导出已审核字段。数量单位：吨。'})
            sheet = book.add_worksheet('材料台账')
            fmt = dict(font_name='Microsoft YaHei', font_size=11, border=1, valign='vcenter', text_wrap=True)
            header = book.add_format(dict(fmt, bg_color='#99CC00', font_color='#FF0000', bold=True, align='center'))
            text = book.add_format(fmt)
            quantity = book.add_format(dict(fmt, num_format='0.000"t"'))
            day = book.add_format(dict(fmt, num_format='yyyy-mm-dd'))
            for i, column in enumerate(COLUMNS):
                sheet.write(1, i, column['label'], header)
            sheet.merge_range(0, 6, 0, 8, '施工现场监督', header)
            for i in [0, 1, 2, 3, 4, 5, 9]:
                sheet.write_blank(0, i, None, header)
            sheet.set_row(0, 23); sheet.set_row(1, 40)
            for i, width in enumerate([16, 21, 34, 34, 17, 24, 26, 16, 27, 22]):
                sheet.set_column(i, i, width)
            sheet.freeze_panes(2, 2)
            sheet.autofilter(1, 0, len(approved_records)+1, len(COLUMNS)-1)
            for index, row in enumerate(approved_records, 2):
                sheet.set_row(index, 42)
                values, _ = validate_values(row['values'], COLUMNS)
                for col, column in enumerate(COLUMNS):
                    value = values[column['key']]
                    if column.get('type') == 'number':
                        sheet.write_number(index, col, float(value), quantity)
                    elif column.get('type') == 'date':
                        sheet.write_datetime(index, col, datetime.strptime(value, '%Y-%m-%d'), day)
                    else:
                        sheet.write_string(index, col, value, text)
                sheet.write_comment(index, 0, f"原文件：{row['pdf_name']}\n铭牌：第{row['sources']['nameplate']['page_no']}页\n检验记录：{', '.join(row['record_ids'])}\n字段审核：{row['confirmed_at']}")
            sheet.set_landscape(); sheet.set_paper(9); sheet.fit_to_pages(1, 0); sheet.repeat_rows(0, 1)
            sources = book.add_worksheet('字段溯源')
            titles = ['台账行', '材料字段', '导出值', 'OCR观察值', '是否补正', '来源表单', 'PDF页码', '选中表单/送货行', '原文', '原PDF坐标', 'OCR转正坐标', '提取方法', '文件', '文件SHA256', '字段确认时间', '审核备注', '坐标源图尺寸', '坐标体系']
            sources.write_row(0, 0, titles, header)
            source_row = 1
            for index, row in enumerate(approved_records, 1):
                for column in COLUMNS:
                    key, role = column['key'], column['role']
                    info = row['fields'][key]
                    evidences = [ev for c in info['candidates'] for ev in c.get('evidence', [])] or [{}]
                    for ev in evidences:
                        actual_role = ev.get('source_role') or info.get('source_role') or role
                        source = row['sources'].get(actual_role, {})
                        page_no = ev.get('page_no') or source.get('page_no')
                        if not page_no and actual_role == 'approval' and row.get('context_id'):
                            page_no = int(row['context_id'].split(':')[-1])
                        sources.write_row(source_row, 0, [index, column['label'], row['values'][key],
                            ' / '.join(c['value'] for c in info['candidates']), '是' if info['corrected'] else '否',
                            dict(approval='报审表',nameplate='铭牌',delivery='送货单',certificate='质量证明书',
                                 inspection='检验记录',mixed='铭牌/送货单').get(actual_role, actual_role),
                            page_no or '', ev.get('cell_id') or ev.get('record_id') or ev.get('row_id') or
                            source.get('row_id') or ev.get('candidate_id') or source.get('candidate_id') or row.get('context_id', ''),
                            ev.get('raw_text') or '', json.dumps(ev.get('polygon_page'), ensure_ascii=False),
                            json.dumps(ev.get('polygon_ocr'), ensure_ascii=False), ev.get('method') or '',
                            row['pdf_name'], row['pdf_sha256'], row['confirmed_at'], row.get('review_note', ''),
                            json.dumps(ev.get('source_pixel_size'), ensure_ascii=False),
                            ev.get('coordinate_system') or '附件 OCR 渲染像素；见审核原图'], text)
                        source_row += 1
            sources.set_column(0, 1, 18); sources.set_column(2, 17, 28); sources.set_column(9, 10, 44)
            sources.freeze_panes(1, 2); sources.autofilter(0, 0, source_row-1, len(titles)-1)
        return output
