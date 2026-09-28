# -*- coding: utf-8 -*-
"""루트 인증서(PEM)를 단말 펌웨어 헤더 FW/Modules/lte_ca.h 로 만든다.

    python -X utf8 Tools/tls/make_ca_header.py [pem 파일]   (기본 Tools/tls/isrgrootx1.pem)

루트 인증서는 공개 자료다(비밀 아님). 단말은 이 PEM 을 모뎀 /data 에 AT*WFPUSH 로 올리고
MQTTS 접속 때 서버 인증서를 이 루트로 검증한다. 서버 인증서(Let's Encrypt)는 90일마다 바뀌어도
루트가 같으면 단말은 손대지 않는다. 루트를 바꿀 때만 이 스크립트를 다시 돌려 펌웨어를 새로 만든다.
"""
import hashlib
import os
import ssl
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'isrgrootx1.pem')
DST = os.path.join(HERE, '..', '..', 'FW', 'Modules', 'lte_ca.h')

pem = open(SRC, encoding='ascii').read().replace('\r\n', '\n')
if not pem.endswith('\n'):
    pem += '\n'
der = ssl.PEM_cert_to_DER_cert(pem)
fp = hashlib.sha256(der).hexdigest().upper()

lines = ['/*',
         ' * lte_ca.h — Tools/tls/make_ca_header.py 가 만든다. 손으로 고치지 않는다.',
         ' *',
         ' * MQTTS 서버 검증용 루트 인증서 (%s)' % os.path.basename(SRC),
         ' * SHA-256 %s' % fp,
         ' * PEM %d 바이트. 공개 인증서라 비밀이 아니다.' % len(pem),
         ' * 모뎀 /data 에 올리는 방법 : MCU 사양서 §14.8, 서버 쪽 준비 : spec/server/TLS_인증서_준비.md',
         ' */',
         '',
         '#ifndef LTE_CA_H_',
         '#define LTE_CA_H_',
         '',
         '#define LTE_CA_NAME\t\t"%s"' % os.path.splitext(os.path.basename(SRC))[0],
         '#define LTE_CA_SHA256\t"%s"' % fp,
         '',
         'static const char lte_ca_pem[] =']
for ln in pem.splitlines():
    lines.append('\t"%s\\n"' % ln)
lines[-1] += ';'
lines += ['', '#endif /* LTE_CA_H_ */', '']
open(DST, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
print('wrote', os.path.normpath(DST), 'PEM', len(pem), 'B', 'SHA256', fp)
