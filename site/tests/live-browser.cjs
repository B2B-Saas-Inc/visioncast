// Run only inside the lifecycle-managed Playwright container; submits synthetic demo data.
const {chromium}=require('playwright');
(async()=>{
const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 const base='https://visioncast-ten.vercel.app';
 await page.goto(base,{waitUntil:'networkidle'});
 if(!(await page.locator('h1').innerText()).trim()) throw Error('No landing heading');
 await page.screenshot({path:'/workspace/Code/webinar-platform/artifacts/live-home.png',fullPage:true});
 await page.goto(base+'/events',{waitUntil:'networkidle'});
 if(await page.locator('a[href="/events/demo-pipeline-signals-launch"]').count()===0) throw Error('Seed event absent');
 await page.goto(base+'/events/demo-pipeline-signals-launch',{waitUntil:'networkidle'});
 await page.getByLabel('Email', {exact:false}).fill(`demo-check-${Date.now()}@example.invalid`);
 await page.getByLabel('First name', {exact:false}).fill('Synthetic');
 await page.getByLabel('Last name', {exact:false}).fill('Demo check');
 const response=page.waitForResponse(r=>r.url().endsWith('/webinar-api/registrations')&&r.request().method()==='POST');
 await page.locator('button[type="submit"]').click();
 const r=await response; if(!r.ok()) throw Error('Registration failed '+r.status()+' '+await r.text());
 await page.waitForTimeout(1000);
 await page.screenshot({path:'/workspace/Code/webinar-platform/artifacts/live-rsvp.png',fullPage:true});
 const body=await r.json(); if(!body.calendar?.icsUrl) throw Error('Missing calendar');
 const ics=await page.request.get(base+body.calendar.icsUrl); if(!ics.ok()||!(await ics.text()).includes('BEGIN:VCALENDAR')) throw Error('Calendar download failed');
 await page.reload({waitUntil:'networkidle'});
 if(!(await page.locator('body').innerText()).includes('registered')) throw Error('Recognition not shown');
 await page.setViewportSize({width:390,height:844});
 await page.goto(base+'/events',{waitUntil:'networkidle'});
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1)) throw Error('Mobile overflow');
 await page.screenshot({path:'/workspace/Code/webinar-platform/artifacts/live-events-mobile.png',fullPage:true});
 await page.goto(base+'/demo-recording/demo-event-ops-interview',{waitUntil:'networkidle'});
 if(!(await page.locator('body').innerText()).toLowerCase().includes('no recording to watch')) throw Error('Demo recording disclosure missing');
 if(errors.length) throw Error('Browser errors: '+errors.join('; '));
 console.log('PASS: landing, live catalogue, RSVP, ICS, cookie recognition, mobile, demo recording, no browser errors');
} finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
