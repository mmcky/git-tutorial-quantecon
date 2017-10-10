# --------------------------------------------
# jupyter Sphinx Extension conversion settings
# --------------------------------------------

# Conversion Mode Settings
# If "all", convert codes and texts into notebook
# If "code", convert codes only
jupyter_conversion_mode = "all"

jupyter_write_metadata = True

# Location for _static folder
jupyter_static_file_path = ["_static"]

# Configure Jupyter Kernels
jupyter_kernels = {
    "python3": {
        "kernelspec": {
            "display_name": "Python",
            "language": "python3",
            "name": "python3"
            },
        "file_extension": ".py",
    },
    "julia": {
        "kernelspec": {
            "display_name": "Julia 0.6.0",
            "language": "julia",
            "name": "julia-0.6"
            },
        "file_extension": ".jl"
    }
}

# Configure jupyter headers
jupyter_headers = {
    "python3": [
        # nbformat.v4.new_code_cell("%autosave 0")      #@mmcky please make this an option
        ],
    "julia": [
        ],
}

# Filename for the file containing the welcome block
jupyter_welcome_block = ""

