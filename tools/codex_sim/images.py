"""Deterministic, wholly synthetic construction scenes and document images.
No source photograph, external model, download or real company branding is used.
"""
import random, math
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pathlib import Path

FONT = Path('C:/Windows/Fonts/msyh.ttc')
def font(n):
    return ImageFont.truetype(str(FONT), n)

def scene(path, idx, category, date, part):
    rng=random.Random(20261011+idx)
    im=Image.new('RGB',(1200,900)); d=ImageDraw.Draw(im)
    # Atmospheric sky and tropical terrain; all shapes are generated locally.
    for y in range(900):
        if y<390:
            t=y/390; col=(int(130+65*t),int(172+42*t),int(205+20*t))
        else:
            t=(y-390)/510; col=(int(126+38*t),int(117+22*t),int(83+24*t))
        d.line((0,y,1200,y),fill=col)
    for _ in range(16):
        x=rng.randrange(-100,1200); y=rng.randrange(60,280)
        d.ellipse((x,y,x+rng.randrange(70,240),y+40),fill=(215,227,231))
    for _ in range(65):
        x=rng.randrange(1200); y=rng.randrange(360,435)
        d.line((x,y+45,x,y-25),fill=(61,79,44),width=3)
        d.ellipse((x-20,y-42,x+22,y),fill=(64,94+rng.randrange(25),57))
    van=590+idx%7*8
    d.polygon([(van-38,410),(van+38,410),(1180,900),(30,900)],fill=(152,143,123))
    d.polygon([(van-12,410),(van+12,410),(1000,900),(195,900)],fill=(101,102,99) if category=='路面' else (167,146,112))
    for j in range(12):
        y=470+j*j*3; x=van+(y-410)*.8
        d.line((x,y,x+20+(y-410)*.07,y+1),fill=(234,216,175),width=3)
    if category in ('桩基','施工过程'):
        # Rotary drilling rig, crawler chassis, pile casing and reinforcement cage.
        x=330+idx%4*55; y=670
        d.rounded_rectangle((x-85,y-18,x+90,y+27),12,fill=(47,50,48))
        for j in range(8): d.ellipse((x-75+j*19,y-10,x-58+j*19,y+12),fill=(95,97,92))
        d.polygon([(x-38,y-120),(x+50,y-120),(x+63,y-18),(x-38,y-18)],fill=(211,168,45))
        d.rectangle((x-19,y-110,x+26,y-58),fill=(60,84,88))
        d.polygon([(x+30,y-30),(x+45,230),(x+65,230),(x+66,y-35)],fill=(208,173,58))
        d.line((x+60,240,x+76,y+80),fill=(53,54,49),width=9)
        d.ellipse((x+38,y+55,x+119,y+95),fill=(72,72,61)); d.rectangle((x+42,y+30,x+115,y+75),fill=(95,103,102))
        for n in range(9):
            d.line((770+n*7,530,785+n*7,750),fill=(66,65,61),width=3)
        for yy in range(540,750,18): d.ellipse((767,yy,847,yy+10),outline=(80,77,68),width=3)
    elif category=='材料':
        for yy in range(4):
            for xx in range(9):
                x=100+xx*40+yy*10; y=580+yy*23
                d.line((x,y,x+250,y+90),fill=(78,79,76),width=7)
        for j in range(6):
            x=690+j%3*110; y=560+j//3*70
            d.rectangle((x,y,x+95,y+60),fill=(206,199,177),outline=(133,128,111),width=2)
            d.text((x+8,y+18),'CEMENT',font=font(13),fill=(74,78,72))
        d.rectangle((60,472,250,515),fill=(225,226,215)); d.text((72,482),'材料分区 · 防潮垫高',font=font(15),fill=(34,57,68))
    elif category=='路面':
        x=450+idx%3*80; y=650
        d.rectangle((x-110,y-100,x+70,y-15),fill=(191,151,40))
        d.rectangle((x-75,y-160,x+15,y-100),fill=(41,63,73))
        d.line((x-42,y-160,x-42,y-215),fill=(64,64,59),width=4)
        d.rectangle((x-125,y-222,x+48,y-210),fill=(63,65,59))
        d.ellipse((x-105,y-50,x-10,y+45),fill=(84,87,84),outline=(48,51,49),width=5)
        d.ellipse((x+4,y-47,x+98,y+40),fill=(84,87,84),outline=(48,51,49),width=5)
    elif category=='桥梁':
        d.polygon([(100,530),(995,455),(1100,505),(120,605)],fill=(165,169,164))
        for x in (260,510,790):
            d.polygon([(x,555),(x+58,548),(x+60,750),(x-4,772)],fill=(145,151,146))
            d.rectangle((x-35,740,x+100,781),fill=(137,140,131))
        for x in range(125,1080,30): d.line((x,532-(x-125)*.07,x,475-(x-125)*.07),fill=(166,89,49),width=4)
        d.line((125,475,1080,408),fill=(203,175,122),width=4)
    else:
        # Before/after safety railing around an excavation, temporary electrical cabinet.
        d.polygon([(160,580),(510,520),(720,700),(380,810)],fill=(83,70,50))
        d.polygon([(160,580),(380,810),(380,740),(190,570)],fill=(116,88,56))
        good=category=='整改'
        for j in range(7 if good else 3):
            x=155+j*62; y=577-j*8
            d.line((x,y,x,y-115),fill=(219,140,42),width=8)
        if good:
            d.line((155,467,535,415),fill=(221,162,56),width=7)
            d.line((155,510,535,458),fill=(221,162,56),width=7)
        d.rectangle((880,490,1050,690),fill=(71,120,144),outline=(40,70,86),width=4)
        d.rectangle((902,522,1029,654),outline=(185,207,209),width=3)
        d.polygon([(947,535),(919,588),(979,588)],fill=(232,209,55))
        d.text((936,552),'!',font=font(27),fill=(30,30,28))
    # Site staff in hard hats; no identifiable real person.
    for j in range(2):
        x=980-j*120+idx%5*8; y=738-j*105; sz=1-j*.2
        d.ellipse((x-14*sz,y-70*sz,x+14*sz,y-42*sz),fill=(124,83,54))
        d.pieslice((x-19*sz,y-83*sz,x+19*sz,y-43*sz),180,360,fill=(238,202,55))
        d.rectangle((x-22*sz,y-43*sz,x+22*sz,y+14*sz),fill=(221,133,35))
        d.line((x-10*sz,y+14*sz,x-15*sz,y+61*sz),fill=(40,58,69),width=10)
        d.line((x+10*sz,y+14*sz,x+19*sz,y+61*sz),fill=(40,58,69),width=10)
        d.line((x-22*sz,y-16*sz,x+22*sz,y-16*sz),fill=(221,231,210),width=5)
    # Texture and tonal variation to prevent copied-image appearance.
    for _ in range(5000):
        x=rng.randrange(1200); y=rng.randrange(440,900)
        d.point((x,y),fill=(rng.randrange(110,170),rng.randrange(100,160),rng.randrange(80,135)))
    d.rectangle((0,0,1200,54),fill=(26,57,140)); d.text((22,10),'仿真公路项目',font=font(25),fill='white')
    d.rectangle((0,816,1200,900),fill=(238,240,236))
    d.text((20,826),f'{part}  ·  {category}',font=font(23),fill=(31,49,70))
    d.text((20,863),f'{date} 09:{idx%60:02d}  |  SIM-{idx:03d}',font=font(19),fill=(69,84,94))
    d.text((985,866),'仿真演示数据',font=font(17),fill=(90,101,109))
    im.save(path,'JPEG',quality=89)

def document(path, idx, category, date, part):
    im=Image.new('RGB',(1200,900),(191,183,165)); d=ImageDraw.Draw(im)
    d.polygon([(180,42),(1000,62),(1040,848),(150,870)],fill=(245,242,227))
    d.text((245,98),'晨洲建材 / Morningland Materials',font=font(28),fill=(43,53,70))
    d.text((350,160),category,font=font(36),fill=(36,42,48))
    rows=[('Document No.',f'SIM-DOC-{idx:03d}'),('Date / 日期',date),('Location / 部位',part),('Description / 品名','C35 预拌混凝土' if category=='混凝土小票' else 'HRB400 钢筋 / 试验送检'),('Batch / 批号',f'SIM-B{idx:03d}'),('Quantity / 数量','8.0 m³' if category=='混凝土小票' else '24.0 t'),('Inspection / 检验','坍落度 160 mm' if category=='混凝土小票' else '按送检批次记录结果'),('Received by / 收货','余青岚'),('Reference / 依据','仿真工程材料台账')]
    y=240
    for k,v in rows:
        d.line((225,y-5,960,y-5),fill=(131,132,122),width=1)
        d.text((235,y),k,font=font(20),fill=(48,54,56)); d.text((535,y),v,font=font(21),fill=(49,62,72)); y+=53
    d.rectangle((230,730,950,805),outline=(119,123,112),width=2)
    d.text((247,753),'Synthetic document · 仿真演示数据',font=font(23),fill=(103,105,98))
    im.save(path,'JPEG',quality=90)
