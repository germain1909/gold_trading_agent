# gold_trading_agent
trading agent v1

# below is how you run it
# adk web agents  




#Setup
python3.10 -m venv ~/venvs/py310
source ~/venvs/py310/bin/activate

# bootstrap/upgrade pip inside the venv
python -m ensurepip --upgrade
python -m pip install --upgrade pip

# confirm
python -m pip --version   # should say (python 3.10)



python3.10 -m venv ~/venvs/py310

- Creates a self-contained Python installation (a virtual environment) in a folder.
- In your case:
  contains its own copy of:
	- the Python 3.10 interpreter
	- its own pip
	- its own site-packages folder (where installed packages live)
👉 This keeps your global system Python totally untouched.
You only have to run this once to create the environment.



source ~/venvs/py310/bin/activate

This “activates” that environment:
- It temporarily changes your PATH so that:
	- python → points to ~/venvs/py310/bin/python
	- pip → points to ~/venvs/py310/bin/pip
- You’ll see your prompt change (like (py310)), so you know you’re “inside” it.
When activated, everything you install or run uses this environment’s Python, not your system one.
To exit, you just run:
```
deactivate
```


python -m ensurepip --upgrade + python -m pip install --upgrade pip

That just installs and updates pip inside your new venv.You only need to do that once, right after creating the environment.


python -m pip --version

Confirms that pip inside the venv works and points to (python 3.10).



After you restart your computer

Your virtual environment doesn’t disappear — it’s just sitting in:
```
~/venvs/py310

```
When you open a new terminal later, to use it again you only need:
```
source ~/venvs/py310/bin/activate
```



Running test
python -m tests.test_yesterday_bar


Running CLI Chat
from topstep.tools import guess_futures_contract
