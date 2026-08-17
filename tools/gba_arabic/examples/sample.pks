# Example script in .pks format. Exercises every markup form:
#   \p paragraph break, \n line break, \l scroll, {PLACEHOLDER}, embedded Latin
#   and digits, and the mandatory lam-alef ligature (لا in MSG_BATTLE).
#
#   python -m tools.gba_arabic scan tools/gba_arabic/examples/sample.pks

[MSG_INTRO]
مرحبا! أنا البروفيسور بيرش.\pهذا العالم مليء بمخلوقات\nتُسمى بوكيمون!

[MSG_NAME]
ما اسمك؟\n{PLAYER}؟ يا له من اسم رائع!

[MSG_LEVEL]
ارتفع {STR_VAR_1} إلى المستوى 50!

[MSG_BATTLE]
لا يمكنك الهرب!\lاختر بوكيمون آخر.

[MSG_MIXED]
سرعة {STR_VAR_1} هي Lv 100 نقطة.
