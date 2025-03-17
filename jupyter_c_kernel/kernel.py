from IPython.core.magic import Magics, magics_class, cell_magic
import tempfile
import subprocess
import os
import re
import threading
import queue

@magics_class
class CMagics(Magics):
    def __init__(self, shell):
        super(CMagics, self).__init__(shell)
        self.files = []
        master_temp = tempfile.mkstemp(suffix='.c')
        master_source = master_temp[1] + '.c'
        os.close(master_temp[0])
        self.master_path = master_temp[1]
        master_code = r"""
#include <stdio.h>
#include <stdlib.h>
#include <dlfcn.h>

typedef int (*run_func)();

int main(int argc, char **argv) {
    if(argc < 2) {
        fprintf(stderr, "Usage: %s <shared_object> [args...]\n", argv[0]);
        return 1;
    }
    void *handle = dlopen(argv[1], RTLD_NOW);
    if(!handle) {
        fprintf(stderr, "Error loading shared object: %s\n", dlerror());
        return 1;
    }
    run_func run = (run_func) dlsym(handle, "run");
    if(!run) {
        fprintf(stderr, "Error finding symbol 'run': %s\n", dlerror());
        return 1;
    }
    int ret = run();
    dlclose(handle);
    return ret;
}
"""
        with open(master_source, 'w') as f:
            f.write(master_code)
        subprocess.check_call(['gcc', master_source, '-std=c11', '-rdynamic', '-ldl', '-o', self.master_path])
        os.remove(master_source)
    
    def cleanup_files(self):
        for f in self.files:
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            os.remove(self.master_path)
        except Exception:
            pass

    def new_temp_file(self, suffix):
        temp_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode='w')
        self.files.append(temp_file.name)
        return temp_file

    def _filter_magics(self, code):
        magics = {'cflags': [], 'ldflags': [], 'args': []}
        for line in code.splitlines():
            if line.startswith('//%'):
                try:
                    key, value = line[3:].split(":", 1)
                    key = key.strip().lower()
                    if key in ['cflags', 'ldflags']:
                        magics[key].extend(value.split())
                    elif key == "args":
                        magics['args'].extend(re.findall(r'(?:[^\s,"]|"(?:\\.|[^"])*")+', value))
                except ValueError:
                    pass
        return magics

    def _real_time_subprocess(self, cmd):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        q_stdout = queue.Queue()
        q_stderr = queue.Queue()

        def enqueue_output(out, q):
            for chunk in iter(lambda: out.read(4096), b''):
                q.put(chunk)
            out.close()
        
        t_out = threading.Thread(target=enqueue_output, args=(proc.stdout, q_stdout))
        t_out.daemon = True
        t_out.start()
        
        t_err = threading.Thread(target=enqueue_output, args=(proc.stderr, q_stderr))
        t_err.daemon = True
        t_err.start()
        
        return proc, q_stdout, q_stderr

    def _print_output(self, q):
        output = b""
        while not q.empty():
            output += q.get_nowait()
        return output.decode()

    @cell_magic
    def c(self, line, cell):
        magics = self._filter_magics(cell)
        with self.new_temp_file('.c') as source_file:
            source_file.write(cell)
            source_file.flush()
            source_filename = source_file.name
        
        with self.new_temp_file('.so') as binary_file:
            binary_filename = binary_file.name
        
        compile_cmd = ['gcc', source_filename, '-std=c11', '-fPIC', '-shared', '-rdynamic'] \
                      + magics['cflags'] + ['-o', binary_filename] + magics['ldflags']
        self.shell.write("Compilando...\n")
        proc_compile, q_stdout, q_stderr = self._real_time_subprocess(compile_cmd)
        while proc_compile.poll() is None:
            self.shell.write(self._print_output(q_stdout))
            self.shell.write(self._print_output(q_stderr))
        self.shell.write(self._print_output(q_stdout))
        self.shell.write(self._print_output(q_stderr))
        if proc_compile.returncode != 0:
            self.shell.write(f"Falha na compilação (código de saída {proc_compile.returncode})\n")
            return
        
        run_cmd = [self.master_path, binary_filename] + magics['args']
        self.shell.write("Executando...\n")
        proc_run, q_stdout, q_stderr = self._real_time_subprocess(run_cmd)
        while proc_run.poll() is None:
            self.shell.write(self._print_output(q_stdout))
            self.shell.write(self._print_output(q_stderr))
        self.shell.write(self._print_output(q_stdout))
        self.shell.write(self._print_output(q_stderr))
    
    def __del__(self):
        self.cleanup_files()

def load_ipython_extension(ipython):
    ipython.register_magics(CMagics)
