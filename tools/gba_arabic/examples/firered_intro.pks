# Arabic translation of FireRed's opening dialogue, as a working starting point.
# Keys mirror the decomp's own string names so they are easy to match up.
#
#   python -m tools.gba_arabic --profile firered scan \
#       tools/gba_arabic/examples/firered_intro.pks
#
# Placeholders are FireRed's, verified from pokefirered's charmap.txt:
#   {PLAYER}=FD 01  {STR_VAR_1}=FD 02  {STR_VAR_2}=FD 03  {STR_VAR_3}=FD 04
#   {RIVAL}=FD 06

[OakSpeech_Text_Hello]
مرحبا!\pآسف لإبقائك في الانتظار!

[OakSpeech_Text_WelcomeToWorldOfPokemon]
أهلا بك في عالم بوكيمون!

[OakSpeech_Text_MyNameIsOak]
اسمي أوك.\pيسمّيني الناس أستاذ بوكيمون.

[OakSpeech_Text_ThisWorldInhabitedByPokemon]
هذا العالم تسكنه مخلوقات\nتُدعى بوكيمون.

[OakSpeech_Text_WhatIsYourName]
ما اسمك؟

[OakSpeech_Text_RightYourNameIs]
صحيح إذن! اسمك {PLAYER}!

[OakSpeech_Text_ThisIsMyGrandchild]
هذا حفيدي.\pاسمه {RIVAL}.

[Text_ObtainedItem]
حصل {PLAYER} على {STR_VAR_1}!

[Text_CantEscape]
لا يمكنك الهرب!

[Text_LevelUp]
ارتفع {STR_VAR_1} إلى المستوى {STR_VAR_2}!

[Text_ItemInPocket]
وضع {STR_VAR_1} في\nجيب {STR_VAR_2}.

[Text_PokedexEntry]
سجّل بيانات بوكيمون في الفهرس.
