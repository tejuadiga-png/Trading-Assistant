#booleen
import operator
from functools import reduce

def combine_flags(df, output_col, input_cols, op="or"):
    """OR/AND/XOR any number of boolean columns into one derived flag.
    Replaces the repeated `df["x"] = (df["a"] | df["b"])` pattern once
    you have more than one or two composite flags to build."""
    fn = {"or": operator.or_, "and": operator.and_, "xor": operator.xor}[op]
    df[output_col] = reduce(fn, (df[c] for c in input_cols))
    return df
