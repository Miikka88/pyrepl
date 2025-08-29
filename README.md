# pyrepl

pyrepl is a small client that turns a remote Python `eval` service into an interactive shell-like interface.
You can test this out with TryHackme's challenge `Pyrat`.

## Features
- Runs commands like `ls`, `pwd`, `cd /tmp`
- Keeps track of current working directory
- Shows remote cwd in the prompt
- Command history with arrow keys. Stored in `~/.pyrepl_history`
- Send raw Python with `:raw ...`
- **Transfers files**:
  - `get <remote> [local]` -> downloads a file
  - `:put <local> [remote]` -> uploads a file
- Quit with `exit`, `quit` or basic `ctrl + c`

## Usage
```bash
python pyrepl.py <target_ip> <target_port>
```

## Example
```bash
/home/ubuntu$ pwd
/home/ubuntu
/home/ubuntu$ ls -al
total 16
drwxr-xr-x  4 root   root   4096 Aug 29 11:34 .
drwxr-xr-x 18 root   root   4096 Aug 29 11:34 ..
drwxr-x---  5 think  think  4096 Jun 21  2023 think
drwxr-xr-x  3 ubuntu ubuntu 4096 Aug 29 11:34 ubuntu
/home/ubuntu$ cd /tmp
/tmp$

/tmp$ :get /etc/passwd
Downloaded xx bytes -> passwd

/tmp$ :put shell.sh /tmp/shell.sh
Uploaded xx bytes -> /tmp/shell.sh

```

## Disclamer
For educational and CTF use only. Do not use on systems that you don't own or have permissions to test.
