"""TableRecognitionPipelineV2 configuration used in the validated single-page lab."""


def model_config(layout=True):
    def module(kind, name, **kw):
        return {"module_name":kind, "model_name":name, "model_dir":None, **kw}
    return {
        "pipeline_name":"table_recognition_v2",
        "use_doc_preprocessor":False, "use_layout_detection":layout, "use_ocr_model":True,
        "SubModules":{
            "LayoutDetection":module("layout_detection","PP-DocLayout-L"),
            "TableClassification":module("table_classification","PP-LCNet_x1_0_table_cls"),
            "WiredTableStructureRecognition":module("table_structure_recognition","SLANeXt_wired"),
            "WirelessTableStructureRecognition":module("table_structure_recognition","SLANeXt_wireless"),
            "WiredTableCellsDetection":module("table_cells_detection","RT-DETR-L_wired_table_cell_det"),
            "WirelessTableCellsDetection":module("table_cells_detection","RT-DETR-L_wireless_table_cell_det"),
        },
        "SubPipelines":{
            "GeneralOCR":{
                "pipeline_name":"OCR", "text_type":"general",
                "use_doc_preprocessor":False, "use_textline_orientation":False,
                "SubModules":{
                    "TextDetection":module("text_detection","PP-OCRv5_server_det",
                        limit_side_len=2048,limit_type="max",max_side_limit=4000,
                        thresh=0.3,box_thresh=0.6,unclip_ratio=1.5),
                    "TextRecognition":module("text_recognition","PP-OCRv5_server_rec",
                        batch_size=8,score_thresh=0),
                },
            },
        },
    }
