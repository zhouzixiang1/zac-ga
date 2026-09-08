# Keep direct latexmk invocations out of the manuscript source tree too.
# The verified entry point is the repository-root `make paper`, which rebuilds
# the standalone overview first and performs the complete evidence audit.
$out_dir = 'build/paper_zh';
$aux_dir = $out_dir;
$pdf_mode = 5;
