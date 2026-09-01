#!/usr/bin/env python3
import json
import subprocess
import re
from pathlib import Path

ACCOUNT='bezouska'
LABEL_FOLDER='Labels/Important'
BATCH=200
ROOT=Path('/root/.openclaw/workspace')
REPORT=ROOT/'tmp'/'bezouska-important-2026-report.json'
QUICK_MAILBOXES=[
    'Labels/Important','Labels/newsletter','Labels/vyresit','Labels/faktury','Labels/finance','Labels/03_ipsd','Labels/50_osobni',
    'Folders/90_ostatni/91_newsletter','Folders/90_ostatni/92_transakce','Folders/10_osobni/11_tomas','Folders/10_osobni/31_zvole','Folders/50_pracovni/51_bezouska','Folders/50_pracovni/52_inadvisors','Folders/50_pracovni/53_ipsd','Folders/50_pracovni/54_mmr','Folders/50_pracovni/70_prazske-noviny','Folders/50_pracovni/80_delta','Folders/99_nezatrideno'
]

def run(*args):
    return subprocess.check_output(list(args), text=True)

def list_env(folder, page='1', page_size='500'):
    return json.loads(run('himalaya','envelope','list','-a',ACCOUNT,'-f',folder,'-p',page,'--page-size',page_size,'--output','json'))

def get_message_id(folder, mid):
    txt=run('himalaya','message','read','-a',ACCOUNT,'-f',folder,'-p','-H','Message-ID',str(mid))
    m=re.search(r'^Message-ID:\s*(.+)$', txt, re.MULTILINE)
    return m.group(1).strip() if m else None

def classify(sender, subj):
    sender=(sender or '').lower(); subj=(subj or '').lower()
    proton=[]
    if sender=='notifications@fibaro.com' or 'fibaro' in sender:
        folder='Folders/10_osobni/31_zvole'; proton+=['vyresit']
    elif sender in ['newsletter@asociace.ai','bingo@patreon.com','contact@blacktailstudio.com','insidercz@substack.com'] or 'newsletter' in sender or ('bloomberg' in sender and 'news' in sender) or 'substack.com' in sender or 'convertkit' in sender or sender=='mesicni@fakturoid.cz':
        folder='Folders/90_ostatni/91_newsletter'; proton+=['newsletter']
    elif sender=='news@ana-white.com':
        folder='Folders/10_osobni/11_tomas'; proton+=['50_osobni']
    elif '@delta-advisory.cz' in sender:
        folder='Folders/50_pracovni/80_delta'
    elif '@eximex.cz' in sender or '@ipsd.cz' in sender:
        folder='Folders/50_pracovni/53_ipsd'; proton+=['03_ipsd']
    elif '@agenturacas.gov.cz' in sender:
        folder='Folders/50_pracovni/51_bezouska'
    elif sender=='info@webglobe.cz':
        folder='Folders/50_pracovni/70_prazske-noviny'
    elif 'mojeid' in sender or '@bezouskova.cz' in sender or sender=='info@brenneroservices.cz':
        folder='Folders/10_osobni/11_tomas'; proton+=['50_osobni']
    elif any(x in subj for x in ['invoice','faktura','platba','payment','výpis z účtu','vypis z uctu','výpis k hypotéce','vypis k hypotéce','hypotéce','hypotecni','doklad']) or sender in ['payments@comgate.cz','info@rb.cz','fakturace@eximex.cz']:
        folder='Folders/90_ostatni/92_transakce'; proton+=['faktury']
    elif sender in ['no-reply@web.opinio.cz','no-reply@tsdb.cz']:
        folder='INBOX'; proton+=['vyresit']
    else:
        folder='INBOX'
    p=[]
    for x in proton:
        if x not in p: p.append(x)
    return folder,p

def gather_hits(sender, subj, msgid):
    hits=[]
    for mailbox in QUICK_MAILBOXES:
        try:
            envs=list_env(mailbox)
        except Exception:
            continue
        for e in envs:
            if e.get('subject')==subj and ((e.get('from') or {}).get('addr')==sender):
                try:
                    m2=get_message_id(mailbox, e['id'])
                except Exception:
                    m2=None
                if m2==msgid:
                    hits.append({'mailbox':mailbox,'id':e['id']})
    return hits

def main():
    envs=list_env(LABEL_FOLDER)
    todo=[e for e in envs if str(e.get('date','')).startswith('2026')][:BATCH]
    processed=[]
    refoldered=0
    checked_existing=0
    removed_important=0
    for e in todo:
        sender=(e.get('from') or {}).get('addr')
        subj=e['subject']
        msgid=get_message_id(LABEL_FOLDER, e['id'])
        hits=gather_hits(sender, subj, msgid)
        folder_hits=[h for h in hits if h['mailbox'].startswith('Folders/')]
        target, proton_labels = classify(sender, subj)
        if folder_hits:
            actual=folder_hits[0]['mailbox']
            checked_existing += 1
            subprocess.run(['himalaya','message','move','-a',ACCOUNT,'-f',LABEL_FOLDER,actual,str(e['id'])], check=True, capture_output=True, text=True)
            for lab in proton_labels:
                subprocess.run(['himalaya','message','copy','-a',ACCOUNT,'-f',actual,f'Labels/{lab}',str(e['id'])], check=True, capture_output=True, text=True)
            removed_important += 1
            processed.append({'subject':subj,'sender':sender,'status':'checked_existing','folder':actual,'labels':proton_labels})
        else:
            subprocess.run(['himalaya','message','move','-a',ACCOUNT,'-f',LABEL_FOLDER,target,str(e['id'])], check=True, capture_output=True, text=True)
            for lab in proton_labels:
                subprocess.run(['himalaya','message','copy','-a',ACCOUNT,'-f',target,f'Labels/{lab}',str(e['id'])], check=True, capture_output=True, text=True)
            refoldered += 1
            removed_important += 1
            processed.append({'subject':subj,'sender':sender,'status':'refoldered','folder':target,'labels':proton_labels})
    remaining2026=len([e for e in list_env(LABEL_FOLDER) if str(e.get('date','')).startswith('2026')])
    report={'processed':len(processed),'refoldered':refoldered,'checked_existing':checked_existing,'removed_important':removed_important,'remaining_2026_in_important':remaining2026,'items':processed[:20]}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'processed':len(processed),'refoldered':refoldered,'checked_existing':checked_existing,'removed_important':removed_important,'remaining_2026_in_important':remaining2026}, ensure_ascii=False))

if __name__=='__main__':
    main()
