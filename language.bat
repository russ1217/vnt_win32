xgettext --language=Python --keyword=_ --output=vnt_helper.pot ./vnt_helper.py
 
rem first time run
msginit --input=vnt_helper.pot --locale=zh_CN.UTF-8 --output-file=vnt_helper.po
rem or
rem update later
msgmerge -U vnt_helper.po vnt_helper.pot
 
msgfmt .\vnt_helper.po -o .\res\locale\zh_CN\LC_MESSAGES\vnt_helper.mo