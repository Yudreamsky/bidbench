"""测试数据：模型空间里排两张图的 DWG（见 README.md）。要 ezdxf。"""
import ezdxf, sys
doc = ezdxf.new("R2000")
doc.header["$DWGCODEPAGE"] = "ANSI_936"
tk = doc.blocks.new("TK")
for a,b in [((0,0),(420,0)),((420,0),(420,297)),((420,297),(0,297)),((0,297),(0,0))]:
    tk.add_line(a,b)
for i,tag in enumerate(["图号","图名","版次","日期","比例"]):
    tk.add_attdef(tag, (330, 40-i*7), dxfattribs={"height":3})
sy = doc.blocks.new("SYM")
sy.add_circle((0,0), 5)
sy.add_attdef("NO", (-2,1), dxfattribs={"height":2.5})
sy.add_attdef("SHEET", (-2,-3), dxfattribs={"height":2.5})
msp = doc.modelspace()
def sheet(x, vals, texts):
    ins = msp.add_blockref("TK", (x,0))
    ins.add_auto_attribs(vals)
    for (tx,ty,t) in texts:
        msp.add_text(t, dxfattribs={"height":3.5, "insert":(x+tx,ty)})
sheet(0, {"图号":"建施-03","图名":"二层平面图","版次":"B","日期":"2026-08-01","比例":"1:100"},
      [(50,250,"二层平面图"),(60,200,"未注明者详见建施-12")])
sheet(500, {"图号":"建施-04","图名":"三层平面图","版次":"A","日期":"2026-07-01","比例":"1:100"}, [(50,250,"三层平面图")])
c = msp.add_blockref("SYM", (100,100)); c.add_auto_attribs({"NO":"5","SHEET":"建施-12"})
msp.add_mtext("{\\fSimSun|b0;墙体做法}\\P见建施-01", dxfattribs={"insert":(80,150)})
lay = doc.layouts.new("布局2")
lay.add_text("布局里的字", dxfattribs={"height":3, "insert":(10,10)})
doc.saveas(sys.argv[1])
