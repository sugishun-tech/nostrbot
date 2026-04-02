system = "あなたはNostrユーザーで、あなたの名前は「ワロタリアン小林」です。できるだけ短く自然な日本語でオヤジ風の口調で2行で返事して。ミルトン・フリードマンの哲学を前提にしてね。現在時間は{cds}"
prompt = "次のテーマについてできるだけ短く自然な日本語で2行でお願い:{theme1} + {theme2}"

with open("themes.txt") as f:
    themes = [line.strip() for line in f if line.strip()]

cs = "はい,承知,ました,です,ます,うーん,あー,ああ,ふーむ,ふん,ふむ,いいだろう,小林,AM,PM"
rmcs = "「,」,*"
