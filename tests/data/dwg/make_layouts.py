"""测试数据：按布局分图的 DWG（见 README.md）。要 ezdxf。"""
import ezdxf, sys
doc = ezdxf.new("R2000")
tb = doc.blocks.new("TB")
for a,b in [((0,0),(841,0)),((841,0),(841,594)),((841,594),(0,594)),((0,594),(0,0))]:
    tb.add_line(a,b)
for i,tag in enumerate(["KD0105","JUDUL1","JUDUL2","SKALA"]):
    tb.add_attdef(tag, (700, 60-i*10), dxfattribs={"height":3})
msp = doc.modelspace()
msp.add_text("CORE A", dxfattribs={"height":300,"insert":(1000,1000)})
msp.add_text("REFER TO A-310 FOR SECTIONS", dxfattribs={"height":300,"insert":(1000,5000)})
msp.add_text("CORE B", dxfattribs={"height":300,"insert":(50000,1000)})
for name, ctr, vals in [("A-701",(2000,2000),{"KD0105":"PRJ1-A-701","JUDUL1":"ENLARGED CORE","JUDUL2":"PLAN A","SKALA":"1:50"}),
                        ("A-702",(51000,2000),{"KD0105":"PRJ1-A-702","JUDUL1":"ENLARGED CORE","JUDUL2":"PLAN B","SKALA":"1:50"})]:
    lay = doc.layouts.new(name)
    ins = lay.add_blockref("TB",(0,0)); ins.add_auto_attribs(vals)
    lay.add_viewport(center=(400,300), size=(600,400), view_center_point=ctr, view_height=8000)
lay = doc.layouts.new("Sheet")
lay.add_text("Sheet No.", dxfattribs={"height":3,"insert":(700,40)})
lay.add_text("A-150", dxfattribs={"height":5,"insert":(730,40)})
lay.add_text("Drawing Title", dxfattribs={"height":3,"insert":(700,60)})
lay.add_text("GENERAL ARRANGEMENT PLAN - LEVEL 12", dxfattribs={"height":5,"insert":(700,52)})
lay.add_text("201/1", dxfattribs={"height":3,"insert":(100,100)})
doc.saveas(sys.argv[1])
