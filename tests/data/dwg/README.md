# LibreDWG 读出的 JSON（合成图纸）

测试用来核对 `bidbench/dwg.py` 认得 LibreDWG 的真实输出格式。图纸全是编的（PRJ1、建施-03 这类），不是任何真实项目。

`model.dwg`、`layouts.dwg` 是对应的 DWG（2000 格式，LibreDWG 的 `dxf2dwg` 写的）：装了 LibreDWG 的电脑上测试会真的读一遍，
安装包打好后的自检（`packaging/smoke.py`）也用它们确认附带的 LibreDWG 能用。

| 文件 | 内容 | 怎么做出来的 |
| --- | --- | --- |
| `model.json` | 模型空间里排两张图（建施-03、建施-04），各有图签块；一个详图索引符号、几段引用文字；另有一个只有一行字的布局 | `make_model.py` |
| `layouts.json` | 两个布局各一张图（PRJ1-A-701、PRJ1-A-702），图签属性名是自己编的（KD0105、JUDUL1/2），视口看模型空间的不同位置；一个布局的图签是普通文字（Sheet No. A-150） | `make_layouts.py` |

做法（要 ezdxf 和 LibreDWG 0.13.3，测试时不需要）：

```
python make_model.py model.dxf
dxf2dwg -y -o model.dwg model.dxf
dwgread -O JSON -o model.json model.dwg
```

`dxf2dwg` 写 DWG 时丢了中文属性名（`model.json` 里图签属性名是空的），正好用来测“属性名认不出、按值认图号”。
