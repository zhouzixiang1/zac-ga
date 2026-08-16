import os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reverse_compiler.gui.app import ReverseCompilerApp
from reverse_compiler.gui.state import Site
errs=[]
def step(app):
    try:
        m=app.state_model; m.reset_atoms(0)
        for a,(r,c) in enumerate([(0,0),(0,1),(0,2),(0,3)]):
            m.init_positions[a]=Site(0,r,c)
        m.atom_count=4
        # drive the parallel move UI form
        app.compose_mode.set("Move"); app._refresh_compose()
        app.move_submode.set("Parallel (AOD)"); app._refresh_move_form()
        app.c_atoms.set("0,1,2,3"); app.c_drow.set("0"); app.c_dcol.set("1")
        app._compose_parallel_move()
        assert len(m.operations)==1 and len(m.operations[0]['end_locs'])==4, m.operations
        app.preview_step=len(m.operations)
        res=app._reverse_compile()
        assert res.ok and res.validation.passed, (res.error, res.validation.as_text())
        print('parallel move UI OK, movement_count=', res.validation.movement_count)
    except Exception as e:
        errs.append(e); traceback.print_exc()
    finally:
        app.after(150, app.destroy)
app=ReverseCompilerApp()
app.after(300, lambda: step(app))
app.after(3000, app.destroy)
app.mainloop()
print('GUI PARALLEL MOVE FORM:', 'FAILED' if errs else 'PASSED')
sys.exit(1 if errs else 0)
