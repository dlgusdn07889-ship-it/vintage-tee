from pathlib import Path
import shutil
ROOT=Path(__file__).resolve().parents[1]
PUBLIC=ROOT/'public'
if PUBLIC.exists(): shutil.rmtree(PUBLIC)
shutil.copytree(ROOT/'site',PUBLIC)
shutil.copytree(ROOT/'data',PUBLIC/'data')
print(f'Built: {PUBLIC}')
