"""
Functionality to deal wth different parameters
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from typing import ClassVar, Dict, List, Literal, Optional, Union

import numpy as np
import pandas as pd
from pydantic import BaseModel, Extra, validator
from sklearn.metrics.pairwise import pairwise_distances

from .utils import (
    check_if_in,
    df_drop_single_value_columns,
    df_uncorrelated_features,
    is_valid_smiles,
    smiles_to_fp_features,
    smiles_to_mordred_features,
    smiles_to_rdkit_features,
)

log = logging.getLogger(__name__)


def _validate_value_list(lst: list, values: dict):
    """A pydantic validator to verify parameter values."""
    if len(lst) < 2:
        raise ValueError(
            f"Parameter {values['name']} must have at least two unique values."
        )
    if len(lst) != len(np.unique(lst)):
        raise ValueError(
            f"Values for parameter {values['name']} are not unique. "
            f"This would cause duplicates in the possible experiments."
        )
    return lst


class Parameter(ABC, BaseModel, extra=Extra.forbid, arbitrary_types_allowed=True):
    """
    Abstract base class for all parameters. Stores information about the
    type, range, constraints, etc. and handles in-range checks, transformations etc.
    """

    # class variables
    type: ClassVar[str]
    SUBCLASSES: ClassVar[Dict[str, Parameter]] = {}

    # object variables
    name: str

    # TODO: Dealing with un-parsed but initialized variables becomes obsolete in
    #  pydantic 2.0 when the __post_init_post_parse__ is available:
    #   - https://github.com/samuelcolvin/pydantic/issues/691
    #   - https://github.com/samuelcolvin/pydantic/issues/1729

    @classmethod
    def create(cls, config: dict) -> Parameter:
        """Creates a new parameter object matching the given specifications."""
        config = config.copy()
        param_type = config.pop("type")
        check_if_in(param_type, list(Parameter.SUBCLASSES.keys()))
        return cls.SUBCLASSES[param_type](**config)

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.SUBCLASSES[cls.type] = cls

    @abstractmethod
    def is_in_range(self, item: object) -> bool:
        """
        Tells whether an item is within the parameter range.
        """

    @property
    @abstractmethod
    def comp_df(self) -> pd.DataFrame:
        """
        Returns the computational representation of the parameter.
        """

    @abstractmethod
    def transform_rep_exp2comp(self, data: pd.Series = None) -> pd.DataFrame:
        """
        Transforms data from experimental to computational representation.

        Parameters
        ----------
        data: pd.Series
            Data to be transformed.

        Returns
        -------
        pd.DataFrame
            The transformed version of the data.
        """


class Categorical(Parameter):
    """
    Parameter class for categorical parameters.
    """

    # class variables
    type = "CAT"

    # object variables
    values: list
    encoding: Literal["OHE", "INT"] = "OHE"

    # validators
    _validated_values = validator("values", allow_reuse=True)(_validate_value_list)

    @property
    def comp_df(self) -> pd.DataFrame:
        """
        See base class.
        """
        # Comp column is identical with the experimental columns

        if self.encoding == "OHE":
            cols = [f"{self.name}_{val}" for val in self.values]
            comp_df = pd.DataFrame(np.eye(len(self.values), dtype=int), columns=cols)
        elif self.encoding == "INT":
            comp_df = pd.DataFrame(
                [k for k, _ in enumerate(self.values)], columns=[self.name]
            )
        else:
            raise ValueError(
                f"The provided encoding '{self.encoding}' is not supported"
            )
        comp_df.index = self.values

        return comp_df

    def is_in_range(self, item: str) -> bool:
        """
        See base class.
        """
        return item in self.values

    def transform_rep_exp2comp(self, data: pd.Series = None) -> pd.DataFrame:
        """
        See base class.
        """

        # Transformation is just lookup for this parameter type
        transformed = pd.merge(
            left=data.rename("Labels").to_frame(),
            left_on="Labels",
            right=self.comp_df,
            right_index=True,
            how="left",
        ).drop(columns="Labels")

        return transformed


class NumericDiscrete(Parameter):
    """
    Parameter class for discrete numerical parameters (a.k.a. setpoints).
    """

    # class variables
    type = "NUM_DISCRETE"

    # object variables
    values: list
    tolerance: float

    # validators
    _validated_values = validator("values", allow_reuse=True)(_validate_value_list)

    @validator("tolerance")
    def validate_tolerance(cls, tolerance, values):
        """
        Validates that the tolerance (i.e. allowed experimental uncertainty when
        reading in measured values) is safe. A tolerance larger than half the minimum
        distance between parameter values is not allowed because that could cause
        ambiguity when inputting datapoints later.
        """
        # NOTE: computing all pairwise distances can be avoided if we ensure that the
        #   values are ordered (which is currently not the case)
        dists = pairwise_distances(np.asarray(values["values"]).reshape(-1, 1))
        np.fill_diagonal(dists, np.inf)
        max_tol = dists.min() / 2.0

        if tolerance >= max_tol:
            raise ValueError(
                f"Parameter {values['name']} is initialized with tolerance "
                f"{tolerance} but due to the values {values['values']} a "
                f"maximum tolerance of {max_tol} is suggested to avoid ambiguity."
            )

        return tolerance

    @property
    def comp_df(self) -> pd.DataFrame:
        """
        See base class.
        """
        # Comp column is identical with the experimental columns
        comp_df = pd.DataFrame({self.name: self.values}, index=self.values)

        return comp_df

    def is_in_range(self, item: float) -> bool:
        """
        See base class.
        """
        differences_acceptable = [
            np.abs(bla - item) <= self.tolerance for bla in self.values
        ]
        return any(differences_acceptable)

    def transform_rep_exp2comp(self, data: pd.Series = None) -> pd.DataFrame:
        """
        See base class.
        """
        # There is nothing to transform for this parameter type
        return data.to_frame()


class GenericSubstance(Parameter):
    """
    Parameter class for generic substances that are treated with cheminformatics
    descriptors. Only a decorrelated subset of descriptors should be sued as otherwise
    there is a large number of features. For a handful of molecules, keeping only
    descriptors that have a max correlation of 0.7 with any other descriptor reduces the
    descriptors to 5-20 ones. This might be substantially more with more labels given
    """

    # class variables
    type = "SUBSTANCE"

    # parsed object variables
    decorrelate: Union[bool, float] = True
    encoding: Literal["MORDRED", "RDKIT", "MORGAN_FP"] = "MORDRED"
    data: Dict[str, str]

    # non-parsed object variables
    values: Optional[list] = None

    @validator("decorrelate", always=True)
    def validate_decorrelate(cls, flag):
        """
        Validates the decorrelate flag
        """
        if isinstance(flag, float):
            if not 0.0 < flag < 1.0:
                raise ValueError(
                    f"The decorrelate flag was set as a float to {flag} "
                    f"but it must be between (excluding) 0.0 and 1.0"
                )

        return flag

    @validator("data", always=True)
    def validate_data(cls, dat):
        """
        Validates the the substances
        """
        for name, smiles in dat.items():
            if not is_valid_smiles(smiles):
                raise ValueError(
                    f"The SMILES '{smiles}' for molecule '{name}' does "
                    f"not appear to be valid."
                )

        return dat

    @validator("values", always=True)
    def validate_values(cls, vals, values):
        """
        Initializes the molecule labels
        """
        if vals is not None:
            raise ValueError(
                "The 'value' option cannot be declared in the config for this "
                "parameter. It is automatically deducted from the 'data' parameter."
            )
        data = values["data"]

        # Since the order of dictionary key is important here, this will only work
        # for Python 3.7 or higher
        return list(data.keys())

    @property
    def comp_df(self) -> pd.DataFrame:
        """
        See base class.
        """
        vals = list(self.data.values())
        names = list(self.data.keys())
        pref = self.name + "_"

        if self.encoding == "MORDRED":
            comp_df = smiles_to_mordred_features(vals, prefix=pref)
        elif self.encoding == "RDKIT":
            comp_df = smiles_to_rdkit_features(vals, prefix=pref)
        elif self.encoding == "MORGAN_FP":
            comp_df = smiles_to_fp_features(vals, prefix=pref)
        else:
            raise ValueError(
                f"The provided encoding '{self.encoding}' is not supported"
            )

        comp_df = df_drop_single_value_columns(comp_df)
        comp_df.index = names
        if self.decorrelate:
            if isinstance(self.decorrelate, bool):
                comp_df = df_uncorrelated_features(comp_df)
            else:
                comp_df = df_uncorrelated_features(comp_df, threshold=self.decorrelate)

        return comp_df

    def is_in_range(self, item: object) -> bool:
        """
        See base class.
        """
        return item in self.values

    def transform_rep_exp2comp(self, data: pd.Series = None) -> pd.DataFrame:
        """
        See base class.
        """

        # Transformation is just lookup for this parameter type
        transformed = pd.merge(
            left=data.rename("Labels").to_frame(),
            left_on="Labels",
            right=self.comp_df,
            right_index=True,
            how="left",
        ).drop(columns="Labels")

        return transformed


class Custom(Parameter):
    """
    Parameter class for custom parameters where the user can read in a precomputed
    representation for labels, e.g. from quantum chemistry.
    """

    # class variables
    type = "CUSTOM"

    # parsed object variables
    decorrelate: Union[bool, float] = True
    data: pd.DataFrame
    identifier_col_idx: int = 0

    # non-parsed object variables
    values: Optional[list] = None

    @validator("decorrelate")
    def validate_decorrelate(cls, flag):
        """
        Validates the decollelate flag
        """
        if isinstance(flag, float):
            if not 0.0 < flag < 1.0:
                raise ValueError(
                    f"The decorrelate flag was set as a float to {flag} "
                    f"but it must be between (excluding) 0.0 and 1.0"
                )

        return flag

    @validator("data")
    def validate_data(cls, data, values):
        """
        Validates the dataframe with the custom representation
        """
        if data.isna().any().any():
            # TODO Tried to trigger this with a Nan, but for some reason the Exception
            #  is not raised. This leads to an error in the identifier_col_idx validator
            #  since the 'data' entry is not in the dict
            raise ValueError(
                f"The custom dataframe for parameter {values['name']} contains NaN "
                f"entries, this is not supported"
            )

        # Always Remove zero variance and non-numeric columns
        # TODO find way to exclude the label column
        # data = df_drop_string_columns(data)
        data = df_drop_single_value_columns(data)

        # TODO Include the feature decorrelation here or somewhere suitable

        return data

    @validator("identifier_col_idx", always=True)
    def validate_identifier_col(cls, col, values):
        """
        Validates the column index which identifies the label column
        """
        if (col < 0) or (col >= len(values["data"].columns)):
            raise ValueError(
                f"identifier_col_idx was {col} but must be a column index between "
                f"(inclusive) 0 and {len(values['data'].columns)-1}"
            )

        return col

    @validator("values", always=True)
    def validate_values(cls, vals, values):
        """
        Initializes the representing labels for this parameter
        """
        if vals is not None:
            raise ValueError(
                "The 'value' option cannot be declared in the config for this "
                "parameter. It is automatically deducted from the 'data' parameter."
            )

        data = values["data"]
        idx_col = values["identifier_col_idx"]

        return data.iloc[:, idx_col].to_list()

    @property
    def comp_df(self) -> pd.DataFrame:
        """
        See base class.
        """
        valcol = self.data.columns[self.identifier_col_idx]
        vals = self.data[valcol].to_list()

        comp_df = self.data.drop(columns=valcol)
        comp_df.index = vals

        return comp_df

    def is_in_range(self, item: object) -> bool:
        """
        See base class.
        """
        return item in self.values

    def transform_rep_exp2comp(self, data: pd.Series = None) -> pd.DataFrame:
        """
        See base class.
        """

        transformed = pd.merge(
            left=data.rename("Labels").to_frame(),
            left_on="Labels",
            right=self.comp_df,
            right_index=True,
            how="left",
        ).drop(columns="Labels")

        return transformed


class NumericContinuous(Parameter, ABC):
    """
    Parameter class for continuous numerical parameters.
    """

    # class variables
    type = "NUM_CONTINUOUS"


def parameter_outer_prod_to_df(
    parameters: List[Parameter],
) -> pd.DataFrame:
    """
    Creates the Cartesion product of all parameter values (ignoring non-discrete
    parameters).

    Parameters
    ----------
    parameters: List[Parameter]
        List of parameter objects.

    Returns
    -------
    pd.DataFrame
        A dataframe containing all parameter value combinations.
    """
    allowed_types = Parameter.SUBCLASSES
    lst_of_values = [p.values for p in parameters if p.type in allowed_types]
    lst_of_names = [p.name for p in parameters if p.type in allowed_types]

    index = pd.MultiIndex.from_product(lst_of_values, names=lst_of_names)
    ret = pd.DataFrame(index=index).reset_index()

    return ret


def scaled_view(
    data_fit: Union[pd.DataFrame, pd.Series],
    data_transform: Union[pd.DataFrame, pd.Series],
    parameters: Optional[List[Parameter]] = None,
    scalers: Optional[dict] = None,
) -> pd.DataFrame:
    """
    Comfort function to scale data given different scaling methods for different
    parameter types.

    Parameters
    ----------
    data_fit : Union[pd.DataFrame, pd.Series]
        Data on which the scalers are fit.
    data_transform : Union[pd.DataFrame, pd.Series]
        Data to be transformed.
    parameters : Optional[List[Parameter]]
        List of baybe parameter objects.
    scalers : Optional[dict]
        Dict with parameter types as keys and sklearn scaler objects as values.

    Returns
    -------
    pd.DataFrame
        The scaled parameter view.

    Examples
    --------
    scalers = {"NUM_DISCRETE": StandardScaler(), "CAT": None}
    scaled_data = scaled_view(
        data_fit = searchspace_comp_rep,
        data_transform = measurements_comp_rep,
        parameters = parameters,
        scalers = scalers,
    )
    """
    transformed = data_transform.copy()
    if parameters is None:
        log.warning("No parameters were provided, not performing any scaling")
        return transformed
    if scalers is None:
        scalers = {}

    for param in parameters:
        if param.comp_df is None:
            # Instead of enforcing this one could automatically detect columns based
            # on the starting of the name.
            raise AttributeError(
                "You are trying to scale parameters that have never used the "
                "transformation from experimental to computational representation. "
                "This means the needed columns cannot be identified."
            )

        # If no scaling instructions provided skip scaling
        if (param.type not in scalers) or (scalers.get(param.type) is None):
            continue

        scaler = scalers.get(param.type)
        if len(param.comp_df.columns) == 1:
            scaler.fit(data_fit[param.comp_df.columns].values.reshape(-1, 1))

            transformed[param.comp_df.columns] = scaler.transform(
                data_transform[param.comp_df.columns].values.reshape(-1, 1)
            )
        else:
            scaler.fit(data_fit[param.comp_df.columns].values)

            transformed[param.comp_df.columns] = scaler.transform(
                data_transform[param.comp_df.columns].values
            )

    return transformed


# TODO self.values could be renamed into something else since its clashing with
#  pydantic enforced syntax, for isntance 'labels' (but thats weird for numeric
#  discrete parameters)

# TODO self.values could be a variable of the base class since its shared between all
#  parameter. Its essentially the list of labels, always one dimensional

# TODO if self.values is part of the base class then is_in_range should also become a
#  method of the base class

# TODO transform rep method can be a non-abstract method of the base class that simply
#  merges the provided series (left) with the comp_df (right) based on a label column.
#  The contents of the label column is defined by self.values
