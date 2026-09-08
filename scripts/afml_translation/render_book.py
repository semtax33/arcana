"""Build self-contained Korean chapter HTML and a truthful book index."""
from pathlib import Path
import json
import re
from html import escape

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'output/afml_ko'

def chapter_filename(n):
    return f'금융_머신러닝의_발전_{n:02d}장_한국어.html'

def load_progress():
    return json.loads((OUT/'translation_progress.json').read_text(encoding='utf-8'))

def render_chapter(n,title,body,toc,pages,abstract,counts,artifacts=None):
    if artifacts:
        for key,val in artifacts.items():body=body.replace('{{'+key+'}}',val)
    assert '{{' not in body, 'Unresolved artifact placeholder'
    original=(OUT/chapter_filename(1)).read_text(encoding='utf-8')
    css=re.search(r'<style>(.*?)</style>',original,re.S).group(1)
    js=re.search(r'<script>(.*?)</script>',original,re.S).group(1)
    extra='''
    .equation{background:#f3f5f8;border:1px solid #e0e5eb;border-radius:6px;margin:24px 0;padding:19px 23px;overflow-x:auto;max-width:100%;white-space:nowrap;font-family:"Cambria Math","Malgun Gothic",serif;font-size:19px;line-height:2.15;letter-spacing:0}.equation sub,.equation sup{font-size:.72em}.equation .indent{padding-left:22px}.code-heading{margin:30px 0 0;padding:13px 19px;background:#2c4559;color:#fff;font-size:13px;border-radius:6px 6px 0 0}pre{margin:0 0 24px;background:#f0f3f6;border:1px solid #d5dfe7;padding:20px;overflow:auto;font-size:13px;line-height:1.8;max-width:100%;word-break:normal;overflow-wrap:normal}pre code{font-family:Consolas,monospace;background:transparent;padding:0;font-size:inherit;white-space:pre;letter-spacing:0}.booklink{display:block;margin-top:14px;color:#d2e8e4}.chapter-footer-links{display:flex;flex-wrap:wrap;gap:18px;margin-top:15px}.status-note{padding:15px 0 0}.intro-map{display:grid;gap:8px;font-size:14px}.chart-note{font-size:12px;color:#718392}.equation::-webkit-scrollbar,pre::-webkit-scrollbar,.svg-scroll::-webkit-scrollbar{height:8px}.equation::-webkit-scrollbar-thumb,pre::-webkit-scrollbar-thumb,.svg-scroll::-webkit-scrollbar-thumb{background:#a8bac7;border-radius:5px}
    @media(max-width:760px){.equation{font-size:17px;padding:14px 16px}pre{font-size:12px;padding:15px}.body .equation{margin-left:-8px;margin-right:-8px;max-width:calc(100% + 16px)}}
    @media print{.equation{font-size:10pt;padding:3mm;white-space:normal;overflow:visible;break-inside:avoid}pre{font-size:7.7pt;white-space:pre-wrap;overflow:visible;break-inside:avoid}pre code{white-space:pre-wrap;overflow-wrap:anywhere}.code-heading{break-after:avoid}a.booklink{display:none}.chapter-footer-links{display:none}}
    '''
    nav=''.join(f'<a href="#{id}"><span>{num}</span>{name}</a>' for id,num,name in toc)
    p0,p1=pages;pdf0,pdf1=p0+27,p1+27
    nexttext=f'다음 장: 제{n+1}장' if n<22 else '본문의 마지막 장'
    html=f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="금융 머신러닝의 발전 제{n}장 한국어 번역. 수식, 코드, SVG 도표와 연습문제 포함."><title>금융 머신러닝의 발전 | 제{n}장 {escape(title)}</title><style>{css}{extra}</style></head><body id="top"><div class="progress" aria-hidden="true"></div><div class="mobilebar"><b>금융 머신러닝의 발전 · {n:02d}</b><button id="menu" aria-expanded="false" aria-controls="sidebar">목차 · 설정</button></div><aside class="sidebar" id="sidebar" aria-label="장 목차와 읽기 설정"><p class="brand">한국어 번역 · 2018년판</p><div class="side-title">금융 머신러닝의<br>발전</div><div class="side-sub">마르코스 로페스 데 프라도<br>제{n}장 · {escape(title)}</div><nav aria-label="제{n}장 목차">{nav}</nav><div class="side-bottom">수록 범위: 인쇄 {p0}–{p1}쪽<br>제공 PDF: {pdf0}–{pdf1}쪽 / 총 393쪽<div class="tools"><button id="font-down" aria-label="본문 글자 줄이기">가 −</button><button id="font-up" aria-label="본문 글자 키우기">가 +</button><button id="print">인쇄</button><button id="notes" aria-pressed="false">해설 상자 숨기기</button></div><p id="font-status" aria-live="polite" style="margin:10px 0 0">본문 18px</p><a class="booklink" href="index.html">전체 목차 · 번역 진행 현황 →</a></div></aside><main><article class="paper"><header class="hero"><div class="eyebrow">금융 머신러닝의 발전 · 제{n}장</div><h1>{escape(title)}</h1><div class="byline">마르코스 로페스 데 프라도 · Wiley, 2018<br>제공된 PDF를 직접 읽고 옮긴 한국어 번역</div><div class="scope"><span>제{n}장 전체 번역</span><span>인쇄 {p0}–{p1}쪽</span><span>도표는 SVG로 재작성</span></div><p class="abstract">{abstract}</p><div class="editorial"><b>읽기 안내</b><br>절 번호·본문·수식·코드·연습문제·참고문헌을 유지했다. 코드는 원문의 계산 흐름을 따라 옮기고 주석은 한국어로 번역했다. 표기·오타를 보완한 곳은 해당 해설에 수정 내역을 적었다. 녹색 상자는 별도로 작성한 해설이며, 원문의 오류로 보이는 부분을 수정한 경우에도 그 사실을 밝혔다. 저자의 통계와 전망은 2018년 저술 시점을 따른다. 영어 원문 본문과 원본 페이지 이미지는 첨부하지 않았다. 이 파일에는 제{n}장이 수록되어 있다.</div></header><div class="book-progress"><a href="index.html">전체 목차와 완성된 장 열기 →</a></div><div class="body">{body}</div><footer><b>제{n}장 끝 · {escape(title)}</b><br>{counts}<br>{nexttext}<div class="chapter-footer-links"><a href="index.html">전체 목차</a><a href="{chapter_filename(n-1)}">← 제{n-1}장</a><a href="#top">처음으로 ↑</a></div></footer></article></main><script>{js}</script></body></html>'''
    target=OUT/chapter_filename(n);target.write_text(html,encoding='utf-8');return target

def mark_complete(n,coverage):
    p=load_progress()
    p['completed_chapters']=sorted(set(p['completed_chapters']+[n]))
    for ch in p['chapters']:
        if ch['number']==n:ch['status']='complete';ch['file']=chapter_filename(n)
    p['chapters'][0]['file']=chapter_filename(1)
    p.setdefault('chapter_coverage',{})[str(n)]=coverage
    if 'coverage' in p:p['chapter_coverage'].setdefault('1',p['coverage'])
    p['next_chapter']=next((i for i in range(1,23) if i not in p['completed_chapters']),None)
    p['main_chapters_complete']=len(p['completed_chapters'])==22
    p['whole_book_complete']=p['main_chapters_complete'] and p.get('whole_book_audit',{}).get('verified',False)
    p['latest_completed_chapter']=n
    p['delivered_file']=chapter_filename(n)
    (OUT/'translation_progress.json').write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
    # Connect verified consecutive chapters without linking to unfinished files.
    for prev in p['completed_chapters']:
        nxt=prev+1
        prevfile=OUT/chapter_filename(prev)
        if nxt not in p['completed_chapters'] or not prevfile.exists():continue
        html=prevfile.read_text(encoding='utf-8')
        old=f'<br>다음 장: 제{nxt}장<div'
        new=f'<br><a href="{chapter_filename(nxt)}">다음 장: 제{nxt}장 →</a><div'
        if old in html:prevfile.write_text(html.replace(old,new),encoding='utf-8')
    write_index(p)

def write_index(p=None):
    p=p or load_progress();items=[]
    for ch in p['chapters']:
        n=ch['number'];done=n in p['completed_chapters'] and (OUT/chapter_filename(n)).exists()
        heading=f'<span class="num">{n:02d}</span><strong>{escape(ch["title_ko"])}</strong><span class="state">{"완료 · 열기 →" if done else "번역 예정"}</span>'
        items.append(f'<a class="chapter done" href="{chapter_filename(n)}">{heading}</a>' if done else f'<div class="chapter pending">{heading}</div>')
    n=len(p['completed_chapters'])
    html=f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>금융 머신러닝의 발전 · 한국어 번역 전체 목차</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#eef2f4;color:#203449;font-family:"Malgun Gothic",sans-serif;word-break:keep-all}}main{{max-width:1050px;margin:45px auto;padding:52px 55px;background:#fffefa;border:1px solid #d7e0e6}}.eyebrow{{font-size:12px;letter-spacing:2px;color:#147a77}}h1{{font-size:39px;line-height:1.45;margin:19px 0}}p{{font-size:15px;line-height:1.9;color:#627687}}.status{{background:#eaf3f0;border-left:4px solid #147a77;padding:18px 22px;margin:27px 0;font-size:15px;line-height:1.9}}.list{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.chapter{{display:flex;gap:14px;align-items:center;min-height:100px;border:1px solid #d6e0e7;padding:20px;text-decoration:none;color:inherit;border-radius:7px;line-height:1.65}}.num{{font-size:21px;color:#648a90;min-width:30px}}.chapter strong{{font-size:15px;flex:1}}.state{{font-size:11px;min-width:75px;color:#147a77}}.pending{{background:#f5f6f6;color:#6b7882}}.pending .state{{color:#84919b}}.done:hover{{border-color:#147a77;background:#f3faf7}}.done:focus-visible{{outline:3px solid #bb7e40}}footer{{border-top:1px solid #d6e0e7;margin-top:30px;padding-top:20px;font-size:12px;line-height:1.9;color:#6c7c86}}@media(max-width:700px){{main{{margin:12px;padding:29px 20px}}h1{{font-size:29px}}.list{{grid-template-columns:1fr}}.chapter{{padding:16px;min-height:82px}}}}</style></head><body><main><div class="eyebrow">마르코스 로페스 데 프라도 · 2018년판</div><h1>금융 머신러닝의 발전<br>한국어 번역</h1><p>제공된 PDF를 장별로 꼼꼼하게 옮기는 번역 작업입니다. 본문과 해설을 구분하고 수식·코드·연습문제를 함께 수록합니다. 그래프와 도표는 각 HTML 안의 SVG로 제공합니다.</p><div class="status"><b>현재 완료: {n} / 22장</b><br>완료된 장은 아래에서 열 수 있습니다. 나머지 장은 아직 번역 중이며 전권 완역은 완료되지 않았습니다.</div><div class="list">{''.join(items)}</div><footer>각 장은 외부 스크립트나 폰트 다운로드 없이 열리는 독립 HTML 파일입니다. 전체 목차와 장 사이 이동을 사용하려면 같은 폴더에 두세요. 원본 PDF 본문이나 페이지 이미지는 결과물에 포함하지 않았습니다.</footer></main></body></html>'''
    if n==22:
        status='본문 22장 번역을 완료했습니다. 앞부분·찾아보기의 번역과 전권 누락 검수를 진행 중입니다.'
        if p.get('whole_book_complete'):status='본문과 부속 자료의 번역 및 전권 누락 검수를 완료했습니다.'
        html=html.replace('완료된 장은 아래에서 열 수 있습니다. 나머지 장은 아직 번역 중이며 전권 완역은 완료되지 않았습니다.',status)
    extras=[]
    for file,label in [('금융_머신러닝의_발전_전권_한국어.html','전권 통합 HTML 열기'),('금융_머신러닝의_발전_앞부분_한국어.html','앞부분 · 추천사 · 상세 목차'),('금융_머신러닝의_발전_찾아보기_한국어.html','찾아보기 · 항목 검색')]:
        if (OUT/file).exists():extras.append(f'<p><a href="{file}" style="color:#147a77;font-weight:bold">{label} →</a></p>')
    if extras:html=html.replace('<div class="list">',''.join(extras)+'<div class="list">')
    if p.get('whole_book_complete'):
        html=html.replace('제공된 PDF를 장별로 꼼꼼하게 옮기는 번역 작업입니다.','제공된 PDF를 장별로 꼼꼼하게 옮긴 한국어 번역본입니다.')
    (OUT/'index.html').write_text(html,encoding='utf-8')

if __name__=='__main__':write_index()
