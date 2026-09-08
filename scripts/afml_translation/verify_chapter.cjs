const {chromium}=require('C:/Users/PC_1M/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),path=require('path');const {pathToFileURL}=require('url');
(async()=>{
const n=Number(process.argv[2]);if(!Number.isInteger(n)||n<1||n>22)throw Error('Expected chapter number');
const dir=`tmp/pdfs/afml_ch${String(n).padStart(2,'0')}`;fs.mkdirSync(dir,{recursive:true});
const file=path.resolve(`output/afml_ko/금융_머신러닝의_발전_${String(n).padStart(2,'0')}장_한국어.html`);
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',args:['--disable-gpu','--disable-extensions','--no-sandbox']});
try{
const page=await browser.newPage({viewport:{width:1440,height:1050},deviceScaleFactor:1});const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.goto(pathToFileURL(file).href);await page.evaluate(()=>document.fonts.ready);await page.addStyleTag({content:'html{scroll-behavior:auto!important}'});
await page.screenshot({path:`${dir}/desktop.png`});
const figureIds=await page.locator('figure').evaluateAll(fs=>fs.map(f=>f.id));
for(const id of figureIds)await page.locator('#'+id).screenshot({path:`${dir}/${id}.png`});
if(await page.locator('.equation').count())await page.locator('.equation').first().screenshot({path:`${dir}/equation.png`});
if(await page.locator('pre').count())await page.locator('pre').first().screenshot({path:`${dir}/code.png`});
const svgBounds=await page.locator('svg').evaluateAll(svgs=>svgs.map(svg=>({title:svg.querySelector('title').textContent,outside:[...svg.querySelectorAll('text')].filter(t=>{const b=t.getBBox(),v=svg.viewBox.baseVal;return b.x<0||b.y<0||b.x+b.width>v.width||b.y+b.height>v.height}).map(t=>t.textContent)})));
const notes=await page.locator('.note:visible').count();await page.locator('#font-up').click();if(await page.locator('#font-status').innerText()!=='본문 19px')throw Error('Font control failed');await page.locator('#font-down').click();
await page.locator('#notes').click();if(await page.locator('.note:visible').count()!==0)throw Error('Hide notes failed');await page.locator('#notes').click();if(await page.locator('.note:visible').count()!==notes)throw Error('Show notes failed');
const navTarget=await page.locator('nav a[href="#exercises"]').count()?'#exercises':'#references';
await page.locator(`nav a[href="${navTarget}"]`).click();if(!page.url().endsWith(navTarget))throw Error('Navigation failed');
await page.setViewportSize({width:390,height:844});await page.evaluate(()=>{location.hash='top';scrollTo({top:0,behavior:'instant'})});await page.waitForTimeout(100);await page.screenshot({path:`${dir}/mobile.png`});
const mobileOverflow=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,viewport:innerWidth}));if(mobileOverflow.scroll>mobileOverflow.viewport)throw Error('Mobile page overflow');
await page.locator('#menu').click();if(!(await page.locator('.sidebar').isVisible()))throw Error('Mobile menu failed');await page.locator(`nav a[href="${navTarget}"]`).click();if(await page.locator('.sidebar').isVisible())throw Error('Menu close failed');
await page.setViewportSize({width:1280,height:1000});await page.emulateMedia({media:'print'});
if(await page.locator('.equation').count())await page.locator('.equation').first().screenshot({path:`${dir}/print_equation.png`});
const links=await page.locator('a[href^="#"]').evaluateAll(as=>as.map(a=>a.getAttribute('href').slice(1)));const ids=await page.locator('[id]').evaluateAll(es=>es.map(e=>e.id));
if(new Set(ids).size!==ids.length)throw Error('Duplicate ID');if(links.some(l=>!ids.includes(l)))throw Error('Broken anchor');
const result={chapter:n,errors,svgBounds,mobileOverflow,notes,figures:figureIds,codeCount:await page.locator('pre').count(),equations:await page.locator('.equation').count(),exerciseCount:await page.locator('.exercise').count(),checks:'Desktop and mobile rendering, SVG text bounds, anchor navigation, menu, font size, notes visibility, print styling'};
fs.writeFileSync(`${dir}/qa.json`,JSON.stringify(result,null,2));console.log(JSON.stringify(result));if(errors.length||svgBounds.some(s=>s.outside.length))process.exitCode=1;
}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
